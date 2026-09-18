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


# Request fields a provider needs that the OpenAI-compatible surface does not
# describe. Sent only to the provider they belong to, because an unknown
# top-level field is accepted by some vendors and rejected outright by others.
#
# DeepSeek: since the V4 rename, thinking mode is on by default at high effort
# on every model name — "flash" is the fast *name*, not a non-thinking model,
# and there is no non-thinking name to switch to. Left on, the model spends the
# whole token budget reasoning and returns empty content, which this codebase
# records as ABSTAIN/NO_JSON: 54% of one day's signals. The debate wants three
# short fields, not a chain of thought, so thinking is turned off rather than
# budgeted for.
_PROVIDER_NO_THINKING = {
    "deepseek": {"thinking": {"type": "disabled"}},
}


def build_llm_client(
    provider: str,
    api_key: str,
    model: str,
    *,
    base_url: str = "",
    thinking: str = "disabled",
):
    """Construct an :class:`LLMClient` for ``provider``.

    Supports Anthropic plus any OpenAI-compatible provider (DeepSeek, Groq,
    Hermes/OpenRouter). Returns a :class:`NullLLMClient` when the provider is
    unknown or no key is available, so callers can always rely on a usable
    client object.

    ``thinking`` is ``"disabled"`` by default, which sends whatever field the
    provider needs to switch reasoning off. Any other value sends nothing and
    leaves the provider's own default in force — the escape hatch if a vendor
    renames the field, since a rejected field would fail every call rather than
    half of them.
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

        return OpenAICompatLLMClient(
            api_key=api_key,
            base_url=resolved_url,
            model=model,
            extra_body=(
                _PROVIDER_NO_THINKING.get(provider, {}) if thinking == "disabled" else {}
            ),
        )

    return NullLLMClient()
