"""OpenAI-compatible LLM client (DeepSeek, Groq, …).

Many providers expose the same ``/chat/completions`` contract as OpenAI, so a
single thin client driven by a ``base_url`` + ``model`` covers all of them. This
implements :class:`~wolf.ai.base.LLMClient` with plain ``requests`` (already a
dependency) instead of pulling the ``openai`` SDK — keeping the install light and
the transport easy to fake in tests.

Used primarily by the flow-intelligence reporter, where DeepSeek writes the
narrative (its prose composition is the strongest of the cheap providers).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from wolf.ai.base import LLMClient

log = logging.getLogger("wolf.ai")

#: Known OpenAI-compatible providers → (base_url, default_model).
PROVIDER_ENDPOINTS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
}


class OpenAICompatLLMClient(LLMClient):
    """Chat-completions client for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._session = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _chat(self, system: str, user: str, *, max_tokens: int, json_mode: bool) -> str:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            payload["temperature"] = 0.2
        try:
            resp = self._session.post(
                f"{self._base}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            log.warning("%s completion failed: %s", self._model, exc)
            return ""

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return self._chat(system, user, max_tokens=max_tokens, json_mode=False)

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        # OpenAI-compatible JSON mode constrains *validity*, not the schema, so we
        # fold the schema into the prompt and parse defensively.
        guided = (
            f"{user}\n\nReturn a single JSON object matching this schema:\n"
            f"{json.dumps(schema)}"
        )
        text = self._chat(system, guided, max_tokens=max_tokens, json_mode=True)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("%s returned non-JSON output: %s", self._model, exc)
            return {}
