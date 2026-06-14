"""AI layer: Bull/Bear debate + arbiter verdict over signal candidates."""

from wolf.ai.base import LLMClient, NullLLMClient
from wolf.ai.debate import Decision, DebateValidator, SignalValidator, Verdict

__all__ = [
    "LLMClient",
    "NullLLMClient",
    "DebateValidator",
    "SignalValidator",
    "Verdict",
    "Decision",
]


def build_llm_client(provider: str, api_key: str, model: str):
    """Construct an :class:`LLMClient` for ``provider``.

    Returns a :class:`NullLLMClient` when the provider is unknown or no key is
    available, so callers can always rely on a usable client object.
    """
    if provider == "anthropic" and api_key:
        from wolf.ai.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(api_key=api_key, model=model)
    return NullLLMClient()
