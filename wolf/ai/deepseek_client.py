"""DeepSeek-backed LLM client.

DeepSeek exposes an OpenAI-compatible REST API, so this implementation uses
``requests`` directly — no extra SDK dependency. The same interface as
:class:`~wolf.ai.anthropic_client.AnthropicLLMClient` means the debate layer
swaps providers with a single config change.

Set ``AI_PROVIDER=deepseek`` and ``DEEPSEEK_API_KEY=sk-...`` to activate.
Model defaults to ``deepseek-chat`` (DeepSeek-V3 / latest stable).
"""

from __future__ import annotations

import json
import logging

import requests

from wolf.ai.base import LLMClient

log = logging.getLogger("wolf.ai")

DEFAULT_MODEL = "deepseek-chat"
_BASE_URL = "https://api.deepseek.com/v1/chat/completions"


class DeepSeekLLMClient(LLMClient):
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._model = model
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    def _call(self, messages: list[dict], max_tokens: int, json_mode: bool = False) -> str:
        payload: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = self._session.post(_BASE_URL, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            log.warning("DeepSeek API error: %s", exc)
            return ""

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return self._call(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens,
        )

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        text = self._call(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens,
            json_mode=True,
        )
        if not text:
            return {}
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("DeepSeek returned non-JSON: %s | raw: %.200s", exc, text)
            return {}
