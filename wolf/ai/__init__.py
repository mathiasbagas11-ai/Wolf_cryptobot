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
    if not api_key:
        return NullLLMClient()
    if provider == "anthropic":
        from wolf.ai.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(api_key=api_key, model=model or "claude-opus-4-8")
    if provider == "deepseek":
        from wolf.ai.deepseek_client import DeepSeekLLMClient

        # Reuse a DeepSeek model if one was configured; otherwise fall back to the
        # default rather than passing a Claude model id through to DeepSeek.
        ds_model = model if model.startswith("deepseek") else "deepseek-chat"
        return DeepSeekLLMClient(api_key=api_key, model=ds_model)
    return NullLLMClient()
