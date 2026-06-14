"""LLM client abstraction.

A minimal, provider-agnostic interface so the AI layer (Bull/Bear debate +
arbiter) does not hard-depend on any single vendor. The concrete
:class:`~wolf.ai.anthropic_client.AnthropicLLMClient` implements it with the
official Anthropic SDK; :class:`NullLLMClient` is the no-op used when no API key
is configured (and in tests). Adding another provider is just another subclass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Two-method LLM surface: free-text and schema-constrained JSON."""

    @abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        """Return the model's text response to ``user`` under ``system``."""
        raise NotImplementedError

    @abstractmethod
    def complete_json(
        self, system: str, user: str, schema: dict, *, max_tokens: int = 1024
    ) -> dict:
        """Return a JSON object conforming to ``schema`` (JSON Schema)."""
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return True


class NullLLMClient(LLMClient):
    """No-op client used when the AI layer is disabled or unconfigured."""

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return ""

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        return {}

    @property
    def available(self) -> bool:
        return False
