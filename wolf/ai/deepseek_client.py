"""DeepSeek-backed LLM client.

Implements :class:`~wolf.ai.base.LLMClient` against DeepSeek's OpenAI-compatible
chat-completions API using plain ``requests`` — no extra SDK dependency, matching
how the rest of the codebase talks to HTTP services. Free-text completions power
the Bull/Bear debate; the arbiter's structured verdict uses DeepSeek's JSON mode
(``response_format={"type": "json_object"}``).

Network and decoding failures are caught narrowly and degrade to an empty result,
so a DeepSeek outage makes the debate ABSTAIN rather than break screening.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from wolf.ai.base import LLMClient

log = logging.getLogger("wolf.ai")

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekLLMClient(LLMClient):
    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _chat(self, system: str, user: str, max_tokens: int, json_mode: bool) -> str:
        payload: dict = {
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
            resp = self._session.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except requests.RequestException as exc:
            log.warning("DeepSeek request failed: %s", exc)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.warning("DeepSeek returned an unexpected payload: %s", exc)
        return ""

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return self._chat(system, user, max_tokens, json_mode=False)

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        # DeepSeek's JSON mode guarantees syntactically valid JSON but not schema
        # conformance, so we spell out the required shape in the prompt and parse
        # defensively. The arbiter then validates/clamps the fields it cares about.
        hint = (
            "\n\nRespond with a single JSON object with exactly these keys: "
            "\"decision\" (one of CONFIRM, NEUTRAL, REJECT), "
            "\"confidence\" (integer 0-100), \"rationale\" (string)."
        )
        text = self._chat(system, user + hint, max_tokens, json_mode=True)
        if not text:
            return {}
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("DeepSeek arbiter returned non-JSON output: %s", exc)
            return {}
