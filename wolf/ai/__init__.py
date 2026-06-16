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


def build_llm_client(provider: str, api_key: str, model: str = ""):
    """Construct an :class:`LLMClient` for ``provider``.

    Supports ``anthropic`` (official SDK) and any OpenAI-compatible provider in
    :data:`~wolf.ai.openai_compat.PROVIDER_ENDPOINTS` (``deepseek``, ``groq``).
    Returns a :class:`NullLLMClient` when the provider is unknown or no key is
    available, so callers can always rely on a usable client object.
    """
    if not api_key:
        return NullLLMClient()
    if provider == "anthropic":
        from wolf.ai.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(api_key=api_key, model=model or "claude-opus-4-8")

    from wolf.ai.openai_compat import PROVIDER_ENDPOINTS, OpenAICompatLLMClient

    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if endpoint is not None:
        base_url, default_model = endpoint
        return OpenAICompatLLMClient(api_key=api_key, base_url=base_url, model=model or default_model)
    return NullLLMClient()
