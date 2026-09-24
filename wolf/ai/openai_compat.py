"""OpenAI-compatible LLM client.

A single client that talks to any provider exposing the OpenAI
``/chat/completions`` API. That covers the cheap models we use for the debate
layer instead of Claude:

* **DeepSeek**  — ``https://api.deepseek.com/v1`` (``deepseek-chat``)
* **Groq**      — ``https://api.groq.com/openai/v1`` (Llama / Mixtral, very fast)
* **Hermes**    — Nous Research Hermes, served OpenAI-style (e.g. via OpenRouter
  ``https://openrouter.ai/api/v1`` with ``nousresearch/hermes-3-llama-3.1-405b``)

Implemented with ``requests`` (already a dependency) so no extra SDK is needed.
JSON output is requested via ``response_format={"type": "json_object"}`` and
defensively parsed — providers that ignore the flag still usually return JSON
because the prompt asks for it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

from wolf.ai.base import LLMClient

log = logging.getLogger("wolf.ai")


def _provider_message(resp: "requests.Response") -> str:
    """The provider's own explanation of an error, trimmed for a log line."""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:300].strip() or "(empty body)"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
        if body.get("message"):
            return str(body["message"])[:300]
    return str(body)[:300]


class OpenAICompatLLMClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30.0,
        extra_headers: Optional[dict] = None,
        extra_body: Optional[dict] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._extra_headers = extra_headers or {}
        #: Provider-specific request fields, merged into every payload. The
        #: OpenAI-compatible surface is a common subset, not a standard, and
        #: the parts that differ are exactly the parts that decide whether a
        #: model answers at all — DeepSeek's thinking mode being the one that
        #: cost this deployment half its verdicts. Keeping the quirk in a dict
        #: the caller supplies lets the provider table carry it, rather than
        #: this client growing a branch per vendor.
        self._extra_body = extra_body or {}
        #: Why the last call failed, verbatim from the provider. Empty after a
        #: success. Read by the startup self-test so a misconfigured key or an
        #: empty balance is named on the deploy log instead of surfacing a day
        #: later as an unbroken run of ABSTAIN verdicts.
        self.last_error: str = ""

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }

    def _chat(self, system: str, user: str, *, max_tokens: int, json_mode: bool) -> str:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # Last, so a provider quirk can override a default set above rather
        # than being silently dropped by it.
        payload.update(self._extra_body)
        try:
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("OpenAI-compat call to %s failed: %s", self._base_url, self.last_error)
            return ""
        if resp.status_code >= 400:
            # The body is the whole diagnosis and raise_for_status discards it.
            # A bare "402 Client Error" reads like a bug in our payload; the
            # body says "Insufficient Balance", which is a billing problem and
            # nothing a code change can fix. Same for an expired key (401) or a
            # rate limit (429) — three very different faults that look
            # identical without it.
            self.last_error = f"HTTP {resp.status_code}: {_provider_message(resp)}"
            log.warning("OpenAI-compat call to %s failed: %s", self._base_url, self.last_error)
            return ""
        try:
            choice = resp.json()["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
        except (KeyError, IndexError, ValueError) as exc:
            self.last_error = f"unreadable response: {type(exc).__name__}: {exc}"
            log.warning("OpenAI-compat call to %s failed: %s", self._base_url, self.last_error)
            return ""

        # A 200 that carries nothing usable is the hardest failure to see: the
        # request was accepted, so there is no status code to blame, and the
        # caller just gets an empty string. The two ways it happens are worth
        # telling apart — a model that spent its budget thinking before it
        # wrote anything, and one that was cut off mid-answer — because the
        # first needs a different model and the second needs a bigger budget.
        finish = choice.get("finish_reason")
        if not content:
            if message.get("reasoning_content"):
                self.last_error = (
                    f"empty content (finish_reason={finish}): the model spent all "
                    f"{max_tokens} tokens on reasoning before answering"
                )
            else:
                self.last_error = f"empty content (finish_reason={finish})"
            log.warning("OpenAI-compat call to %s: %s", self._base_url, self.last_error)
            return ""
        if finish == "length":
            self.last_error = f"answer cut off at max_tokens={max_tokens}"
            log.warning("OpenAI-compat call to %s: %s", self._base_url, self.last_error)
            return content
        self.last_error = ""
        return content

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return self._chat(system, user, max_tokens=max_tokens, json_mode=False)

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        # Nudge the model toward the schema; response_format enforces JSON shape.
        sys = f"{system}\n\nReturn ONLY a JSON object matching this schema:\n{json.dumps(schema)}"
        text = self._chat(sys, user, max_tokens=max_tokens, json_mode=True)
        if not text:
            return {}
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # Some models wrap JSON in prose/fences — grab the first {...} block.
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            # Quote what did come back. "No JSON" describes every one of these
            # equally, and the first line of the actual reply usually says
            # which it is at a glance. A truncation already diagnosed upstream
            # is the more specific answer and keeps precedence: unparseable is
            # what being cut off looks like, not a second, separate fault.
            if not self.last_error:
                self.last_error = f"reply was not JSON: {text[:160].strip()!r}"
            log.warning("Arbiter returned non-JSON output from %s: %s",
                        self._base_url, self.last_error)
            return {}
