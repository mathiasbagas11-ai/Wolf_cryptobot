"""Anthropic-backed LLM client.

Implements :class:`~wolf.ai.base.LLMClient` with the official ``anthropic`` SDK.
Defaults to ``claude-opus-4-8`` and uses adaptive thinking for the (genuinely
non-trivial) arbiter reasoning. Structured verdicts go through the Messages API
``output_config.format`` JSON-schema constraint so the arbiter always returns a
parseable object.

The ``anthropic`` import is lazy (inside ``__init__``) so the rest of the app —
and the test suite — does not require the SDK to be installed unless the AI
layer is actually constructed.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from wolf.ai.base import LLMClient

log = logging.getLogger("wolf.ai")

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        *,
        use_thinking: bool = True,
    ) -> None:
        try:
            import anthropic  # lazy: only needed when the AI layer is enabled
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "The 'anthropic' package is required for the AI layer. "
                "Install it with: pip install anthropic"
            ) from exc

        self._anthropic = anthropic
        # An empty api_key lets the SDK resolve ANTHROPIC_API_KEY from the env.
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._model = model
        self._use_thinking = use_thinking

    def _thinking(self) -> Optional[dict]:
        return {"type": "adaptive"} if self._use_thinking else {"type": "disabled"}

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                thinking=self._thinking(),
                messages=[{"role": "user", "content": user}],
            )
        except self._anthropic.APIError as exc:
            log.warning("Anthropic completion failed: %s", exc)
            return ""
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                thinking=self._thinking(),
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.APIError as exc:
            log.warning("Anthropic JSON completion failed: %s", exc)
            return {}
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("Arbiter returned non-JSON output: %s", exc)
            return {}
