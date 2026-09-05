"""Tests for the AI debate layer and its screener integration."""

from __future__ import annotations

from wolf.ai import DebateValidator, NullLLMClient, build_llm_client
from wolf.ai.base import LLMClient
from wolf.ai.debate import Decision
from wolf.config import Settings
from wolf.detectors import MomentumBreakoutDetector
from wolf.detectors.base import SignalCandidate
from wolf.models import Candle
from wolf.screener import Screener
from wolf.tracker import Tracker


class FakeLLM(LLMClient):
    """Scriptable LLM client — no network, deterministic verdicts."""

    def __init__(self, decision: str = "CONFIRM", confidence: int = 80) -> None:
        self._decision = decision
        self._confidence = confidence
        self.calls: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        self.calls.append(system[:10])
        return "bull or bear argument"

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int = 1024) -> dict:
        return {"decision": self._decision, "confidence": self._confidence, "rationale": "test rationale"}


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        symbol="BTCUSDT", signal_type="SCREENER", direction="LONG",
        entry_price=100, tp=110, sl=95, score=80, strategy="MOMENTUM",
        reasons=["breakout"], tps=[{"level": 1, "price": 110}],
    )


# ── client plumbing ───────────────────────────────────────────────────────
def test_null_client_unavailable():
    assert NullLLMClient().available is False


def test_build_llm_client_falls_back_to_null_without_key():
    client = build_llm_client("anthropic", api_key="", model="claude-opus-4-8")
    assert client.available is False


def test_build_llm_client_unknown_provider_is_null():
    assert build_llm_client("acme", "key", "m").available is False


def test_build_llm_client_openai_compat_providers():
    from wolf.ai.openai_compat import OpenAICompatLLMClient

    for provider in ("deepseek", "groq", "hermes"):
        client = build_llm_client(provider, "key", "model")
        assert isinstance(client, OpenAICompatLLMClient)
        assert client.available is True


def test_debate_splits_roles_across_clients():
    bull, bear, arbiter = FakeLLM(), FakeLLM(), FakeLLM("REJECT", 90)
    verdict = DebateValidator(bull=bull, bear=bear, arbiter=arbiter).validate(_candidate())
    assert verdict.decision == Decision.REJECT
    assert len(bull.calls) == 1 and len(bear.calls) == 1  # one role each


def test_debate_abstains_when_all_roles_unavailable():
    null = NullLLMClient()
    v = DebateValidator(bull=null, bear=null, arbiter=null).validate(_candidate())
    assert v.decision == Decision.ABSTAIN


# ── debate ────────────────────────────────────────────────────────────────
def test_validator_abstains_when_unavailable():
    verdict = DebateValidator(NullLLMClient()).validate(_candidate())
    assert verdict.decision == Decision.ABSTAIN


def test_validator_confirm():
    verdict = DebateValidator(FakeLLM("CONFIRM", 85)).validate(_candidate())
    assert verdict.decision == Decision.CONFIRM
    assert verdict.confidence == 85
    assert verdict.rationale == "test rationale"


def test_validator_runs_bull_and_bear():
    fake = FakeLLM("NEUTRAL", 50)
    DebateValidator(fake).validate(_candidate())
    assert len(fake.calls) == 2  # bull + bear free-text calls


def test_validator_clamps_confidence():
    class Over(FakeLLM):
        def complete_json(self, *a, **k):
            return {"decision": "CONFIRM", "confidence": 250, "rationale": "x"}

    verdict = DebateValidator(Over()).validate(_candidate())
    assert verdict.confidence == 100


def test_validator_handles_malformed_json():
    class Bad(FakeLLM):
        def complete_json(self, *a, **k):
            return {}

    verdict = DebateValidator(Bad()).validate(_candidate())
    assert verdict.decision == Decision.ABSTAIN


# ── screener integration (monitor mode — AI labels, never blocks) ──────────
def _breakout_candles() -> list[Candle]:
    cs = [Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0) for i in range(60)]
    cs.append(Candle(time=60 * 900_000, open=100, high=108, low=100, close=107, volume=500.0))
    return cs


def _screener(store, fake_client, tracker_settings, validator):
    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)
    return Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None,
        universe=["BTCUSDT"], validator=validator, veto_min_confidence=70,
    ), tracker


def test_reject_high_confidence_is_flagged_not_blocked(store, fake_client, tracker_settings):
    """Monitor mode: a high-confidence REJECT still emits the signal, flagged ai_vetoed."""
    screener, tracker = _screener(store, fake_client, tracker_settings, DebateValidator(FakeLLM("REJECT", 90)))
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    sig = recorded[0]
    assert sig.ai_verdict == "REJECT"
    assert sig.ai_confidence == 90
    assert sig.ai_vetoed is True
    assert tracker.active_signals() != []  # signal is tracked, not dropped


def test_reject_low_confidence_not_flagged(store, fake_client, tracker_settings):
    """A low-confidence REJECT (below threshold) is recorded without the veto flag."""
    screener, tracker = _screener(store, fake_client, tracker_settings, DebateValidator(FakeLLM("REJECT", 40)))
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].ai_verdict == "REJECT"
    assert recorded[0].ai_vetoed is False


def test_confirm_keeps_signal_and_stores_verdict(store, fake_client, tracker_settings):
    screener, tracker = _screener(store, fake_client, tracker_settings, DebateValidator(FakeLLM("CONFIRM", 85)))
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    sig = recorded[0]
    assert sig.ai_verdict == "CONFIRM"
    assert sig.ai_confidence == 85
    assert sig.ai_rationale == "test rationale"
    assert sig.ai_vetoed is False


def test_no_validator_leaves_ai_fields_empty(store, fake_client, tracker_settings):
    """With no AI configured, signals carry empty AI fields (backward compat)."""
    screener, tracker = _screener(store, fake_client, tracker_settings, None)
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].ai_verdict == ""
    assert recorded[0].ai_vetoed is False


# ── availability: the arbiter is the load-bearing role ──────────────────────
class _EchoClient(LLMClient):
    """A usable client: free text, and a well-formed verdict."""

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return "argument"

    def complete_json(self, system, user, schema, *, max_tokens: int = 1024) -> dict:
        return {"decision": "CONFIRM", "confidence": 80, "rationale": "ok"}


def test_live_bull_with_dead_arbiter_is_not_available():
    """The exact production failure: one key set, three providers configured.

    bull=deepseek had a key; bear=groq and arbiter=hermes did not. `any()` across
    the roles reported the layer healthy, so validate() ran, the null arbiter
    returned {}, and every signal abstained — silently.
    """
    v = DebateValidator(bull=_EchoClient(), bear=NullLLMClient(), arbiter=NullLLMClient())
    assert v.available is False
    assert v.degraded_roles == ["bear", "arbiter"]


def test_available_tracks_the_arbiter_only():
    v = DebateValidator(bull=NullLLMClient(), bear=NullLLMClient(), arbiter=_EchoClient())
    assert v.available is True          # degraded, but it can still decide
    assert v.degraded_roles == ["bull", "bear"]


def test_silent_arbiter_abstain_is_logged(caplog):
    """An arbiter returning no JSON must never abstain without saying so."""

    class _NoJson(_EchoClient):
        def complete_json(self, system, user, schema, *, max_tokens: int = 1024) -> dict:
            return {}

    v = DebateValidator(bull=_EchoClient(), bear=_EchoClient(), arbiter=_NoJson())
    with caplog.at_level("WARNING"):
        verdict = v.validate(_candidate())
    assert verdict.decision == Decision.ABSTAIN
    assert "no verdict JSON" in caplog.text


def test_all_debate_roles_default_to_one_provider(monkeypatch):
    """A single DEEPSEEK_API_KEY must be enough, as the docs have always said.

    from_env previously defaulted bear to groq and arbiter to hermes, quietly
    requiring three keys.
    """
    for var in ("DEBATE_BULL_PROVIDER", "DEBATE_BEAR_PROVIDER", "DEBATE_ARBITER_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_DEBATE_ENABLED", "true")
    ai = Settings.from_env().ai
    assert ai.bull.provider == ai.bear.provider == ai.arbiter.provider == "deepseek"


# ── the arbiter's token budget is reachable without a redeploy ──────────────


class _BudgetSpy(LLMClient):
    """Records the budget every call was actually made with."""

    def __init__(self) -> None:
        self.budgets: list[int] = []

    @property
    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return "case"

    def complete_json(self, system, user, schema, *, max_tokens: int = 1024) -> dict:
        self.budgets.append(max_tokens)
        return {"decision": "NEUTRAL", "confidence": 0, "rationale": "ok"}


def test_the_configured_budget_reaches_the_arbiter_call():
    """A setting that never reaches the API call is the failure mode here.

    The budget only matters at the moment the request is sent, so wiring it as
    far as the validator and no further would read as configurable while every
    call still used the default.
    """
    spy = _BudgetSpy()
    DebateValidator(arbiter=spy, arbiter_max_tokens=777).validate(_candidate())
    assert spy.budgets == [777]


def test_the_boot_probe_uses_the_same_budget_as_a_live_verdict():
    """Otherwise the probe stops testing the path it exists to test.

    A probe on the default budget would pass while every real signal abstained
    at the configured one — the exact silence the self-test was added to break.
    """
    spy = _BudgetSpy()
    validator = DebateValidator(arbiter=spy, arbiter_max_tokens=777)
    validator.selftest()
    validator.validate(_candidate())
    assert spy.budgets == [777, 777]


def test_the_budget_defaults_to_the_module_constant():
    from wolf.ai.debate import _ARBITER_MAX_TOKENS

    spy = _BudgetSpy()
    DebateValidator(arbiter=spy).validate(_candidate())
    assert spy.budgets == [_ARBITER_MAX_TOKENS]


def test_the_budget_is_settable_from_the_environment(monkeypatch):
    from wolf.config import Settings

    monkeypatch.setenv("DEBATE_ARBITER_MAX_TOKENS", "4096")
    assert Settings.from_env().ai.arbiter_max_tokens == 4096
    monkeypatch.delenv("DEBATE_ARBITER_MAX_TOKENS")
    assert Settings.from_env().ai.arbiter_max_tokens == 2048


# ── thinking mode, and the roles that answer with nothing ───────────────────


class _Resp:
    """Minimal stand-in for the provider's HTTP response."""

    status_code = 200

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


def test_deepseek_is_told_not_to_think(monkeypatch):
    """The fault this exists to stop, at the layer that can stop it.

    Since the V4 rename every DeepSeek model name reasons by default at high
    effort — "flash" is the fast name, not a non-thinking model, and there is
    no non-thinking name to move to. Left alone the model spends the whole
    budget reasoning and returns empty content, which the card records as
    ABSTAIN/NO_JSON.
    """
    sent = {}
    client = build_llm_client("deepseek", "k", "deepseek-v4-flash")
    monkeypatch.setattr(
        "wolf.ai.openai_compat.requests.post",
        lambda *a, **kw: sent.update(kw["json"]) or _Resp({"choices": [
            {"message": {"content": "hi"}}
        ]}),
    )
    client.complete("sys", "user")
    assert sent["thinking"] == {"type": "disabled"}


def test_the_field_goes_only_to_the_provider_that_owns_it():
    """An unknown top-level field is ignored by some vendors and rejected by others.

    Sending DeepSeek's switch to Groq or OpenRouter would risk turning a layer
    that fails half the time into one that fails every time.
    """
    assert build_llm_client("groq", "k", "m")._extra_body == {}
    assert build_llm_client("hermes", "k", "v/m")._extra_body == {}


def test_thinking_can_be_put_back_without_a_deploy():
    """The way out if the vendor renames the field.

    A rejected field fails every call rather than half of them, so the escape
    hatch has to be reachable from the environment.
    """
    assert build_llm_client("deepseek", "k", "m", thinking="enabled")._extra_body == {}


def test_the_thinking_switch_is_settable_from_the_environment(monkeypatch):
    from wolf.config import Settings

    assert Settings.from_env().ai.thinking == "disabled"
    monkeypatch.setenv("AI_THINKING", "ENABLED")
    assert Settings.from_env().ai.thinking == "enabled"  # case-folded


def test_a_provider_quirk_cannot_be_dropped_by_a_default(monkeypatch):
    """Merged last, so it overrides rather than being overridden."""
    sent = {}
    client = build_llm_client("deepseek", "k", "m")
    monkeypatch.setattr(
        "wolf.ai.openai_compat.requests.post",
        lambda *a, **kw: sent.update(kw["json"]) or _Resp({"choices": [
            {"message": {"content": '{"decision": "NEUTRAL"}'}}
        ]}),
    )
    client.complete_json("sys", "user", {"type": "object"})
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["thinking"] == {"type": "disabled"}


class _SilentSide(LLMClient):
    """A bull or bear whose client works and whose answer is empty."""

    @property
    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return "   "

    def complete_json(self, system, user, schema, *, max_tokens: int = 1024) -> dict:
        return {}


class _Arbiter(LLMClient):
    @property
    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        return "case"

    def complete_json(self, system, user, schema, *, max_tokens: int = 1024) -> dict:
        return {"decision": "NEUTRAL", "confidence": 0, "rationale": "ok"}


def test_a_side_that_answers_with_nothing_is_named():
    """The quietest failure in the layer, and the one nothing downstream sees.

    An empty argument is not an error on any path: it becomes "(none)" in the
    arbiter's prompt, the verdict still comes back, and the card reports a
    healthy debate that was the arbiter talking to itself.
    """
    result = DebateValidator(
        arbiter=_Arbiter(), bull=_SilentSide(), bear=_Arbiter()
    ).selftest()
    assert result["ok"] is True          # the arbiter still answers
    assert result["silent_roles"] == ["bull"]   # and the debate is still broken


def test_a_role_with_no_client_is_not_also_called_silent():
    """degraded_roles already reports it; counting it twice reads as two faults."""
    result = DebateValidator(arbiter=_Arbiter()).selftest()
    assert result["silent_roles"] == []
    assert DebateValidator(arbiter=_Arbiter()).degraded_roles == ["bull", "bear"]


def test_silent_roles_join_the_list_the_card_already_prints():
    """No second channel a reader has to know to look at."""
    from types import SimpleNamespace

    from wolf.app import ai_status
    from wolf.config import Settings

    app = SimpleNamespace(
        settings=Settings(),
        screener=SimpleNamespace(_validator=DebateValidator(
            arbiter=_Arbiter(), bull=_SilentSide(), bear=_Arbiter(),
        )),
    )
    status = ai_status(app, probe=True)
    assert status["available"] is True
    assert "bull" in status["degraded_roles"]


class _SilentWithError(_SilentSide):
    """A side that answered with nothing and knows why."""

    last_error = "HTTP 429: Rate limit reached"


def test_a_silent_role_carries_the_provider_s_own_explanation():
    """Four unrelated faults share one symptom, and they share no remedy.

    A rate limit, a spent balance, a budget eaten by reasoning and a model that
    answered with whitespace all read as "bear is quiet". The provider already
    said which one it was — the only job is not to throw that away.
    """
    result = DebateValidator(
        arbiter=_Arbiter(), bull=_Arbiter(), bear=_SilentWithError()
    ).selftest()
    assert result["silent_roles"] == ["bear"]
    assert result["silent_reasons"] == {"bear": "HTTP 429: Rate limit reached"}


def test_a_silent_role_with_no_error_still_says_something():
    """Empty is itself a finding — the model replied, with nothing in it."""
    result = DebateValidator(
        arbiter=_Arbiter(), bull=_Arbiter(), bear=_SilentSide()
    ).selftest()
    assert result["silent_reasons"] == {"bear": "returned empty content"}


def test_the_reason_reaches_the_telegram_card():
    """Otherwise the reader is sent back to the logs to find out which fault it was."""
    from types import SimpleNamespace

    from wolf.notify.commands import CommandRouter

    from dataclasses import replace

    settings = Settings()
    app = SimpleNamespace(
        analyze=None, account=None, learning=None, tracker=None,
        settings=replace(settings, ai=replace(settings.ai, enabled=True)),
        screener=SimpleNamespace(_validator=DebateValidator(
            arbiter=_Arbiter(), bull=_Arbiter(), bear=_SilentWithError(),
        )),
    )
    reply = CommandRouter(app).handle("/ai")
    assert "Weak roles: bear" in reply
    assert "Rate limit reached" in reply
