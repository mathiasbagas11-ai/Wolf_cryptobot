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


# OpenAI-compatible provider presets: base URL for the /chat/completions API.
# Hermes is reached through OpenRouter by default (cheap, hosts Nous models).
_OPENAI_COMPAT_PRESETS = {
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "hermes": "https://openrouter.ai/api/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def build_llm_client(provider: str, api_key: str, model: str, *, base_url: str = ""):
    """Construct an :class:`LLMClient` for ``provider``.

    Supports Anthropic plus any OpenAI-compatible provider (DeepSeek, Groq,
    Hermes/OpenRouter). Returns a :class:`NullLLMClient` when the provider is
    unknown or no key is available, so callers can always rely on a usable
    client object.
    """
    provider = (provider or "").lower()
    if not api_key:
        return NullLLMClient()

    if provider == "anthropic":
        from wolf.ai.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(api_key=api_key, model=model)

    resolved_url = base_url or _OPENAI_COMPAT_PRESETS.get(provider)
    if resolved_url:
        from wolf.ai.openai_compat import OpenAICompatLLMClient

        return OpenAICompatLLMClient(api_key=api_key, base_url=resolved_url, model=model)

    return NullLLMClient()
