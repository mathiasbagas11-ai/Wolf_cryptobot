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


class OpenAICompatLLMClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30.0,
        extra_headers: Optional[dict] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._extra_headers = extra_headers or {}

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
        try:
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            log.warning("OpenAI-compat call to %s failed: %s", self._base_url, exc)
            return ""

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
            log.warning("Arbiter returned non-JSON output from %s", self._base_url)
            return {}
