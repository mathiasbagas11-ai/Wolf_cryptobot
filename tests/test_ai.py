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
