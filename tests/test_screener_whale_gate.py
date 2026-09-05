"""Tests for the whale veto gate and the on-chain context injected into the debate.

The gate sits in the screener, not in a detector: detectors stay pure functions
of candles plus context, and a test here proves ``Detector.evaluate`` still
produces the candidate that the gate then drops.
"""

from __future__ import annotations

from wolf.ai.debate import _describe, _onchain_lines
from wolf.detectors.base import SignalCandidate
from wolf.market import MarketContext
from wolf.screener import Screener


def _candidate(direction: str = "LONG", symbol: str = "SOLUSDT") -> SignalCandidate:
    return SignalCandidate(
        symbol=symbol,
        signal_type="MOMENTUM",
        direction=direction,
        entry_price=100.0,
        tp=110.0,
        sl=95.0,
        score=80,
        confluence_level="MEDIUM",
        reasons=["breakout confirmed"],
        strategy="MOMENTUM",
    )


def _screener(client, tracker, **kw) -> Screener:
    return Screener(client, tracker, [], **kw)


# ── the veto rule ─────────────────────────────────────────────────────────
def test_strong_opposing_coordination_vetoes(fake_client, tracker):
    screener = _screener(fake_client, tracker, whale_veto_min_wallets=5)
    ctx = MarketContext(whale_coordination="SHORT", whale_wallet_count=6)

    assert screener._whale_vetoed(_candidate("LONG"), ctx)


def test_agreeing_coordination_does_not_veto(fake_client, tracker):
    screener = _screener(fake_client, tracker, whale_veto_min_wallets=5)
    ctx = MarketContext(whale_coordination="LONG", whale_wallet_count=9)

    assert not screener._whale_vetoed(_candidate("LONG"), ctx)


def test_weak_opposing_coordination_does_not_veto(fake_client, tracker):
    """3 wallets is worth an alert; overriding a technical setup takes 5."""
    screener = _screener(fake_client, tracker, whale_veto_min_wallets=5)
    ctx = MarketContext(whale_coordination="SHORT", whale_wallet_count=3)

    assert not screener._whale_vetoed(_candidate("LONG"), ctx)


def test_short_candidate_vetoed_by_opposing_longs(fake_client, tracker):
    screener = _screener(fake_client, tracker, whale_veto_min_wallets=5)
    ctx = MarketContext(whale_coordination="LONG", whale_wallet_count=5)

    assert screener._whale_vetoed(_candidate("SHORT"), ctx)


def test_no_whale_data_never_vetoes(fake_client, tracker):
    """Stale or absent data must degrade to the previous behaviour, not block."""
    screener = _screener(fake_client, tracker)

    assert not screener._whale_vetoed(_candidate("LONG"), MarketContext())
    assert not screener._whale_vetoed(_candidate("LONG"), None)


def test_gate_can_be_disabled(fake_client, tracker):
    screener = _screener(fake_client, tracker, whale_veto_enabled=False)
    ctx = MarketContext(whale_coordination="SHORT", whale_wallet_count=20)

    assert not screener._whale_vetoed(_candidate("LONG"), ctx)


def test_threshold_is_configurable(fake_client, tracker):
    ctx = MarketContext(whale_coordination="SHORT", whale_wallet_count=3)

    assert _screener(fake_client, tracker, whale_veto_min_wallets=3)._whale_vetoed(_candidate(), ctx)
    assert not _screener(fake_client, tracker, whale_veto_min_wallets=4)._whale_vetoed(_candidate(), ctx)


# ── gate ordering: cheap veto before the expensive debate ─────────────────
class _CountingValidator:
    """Records how often the AI debate was actually invoked."""

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, candidate, context=None, candles=(), tf_candles={}):
        self.calls += 1
        from wolf.ai.debate import Decision, Verdict
        return Verdict(decision=Decision.CONFIRM, confidence=90)


def test_vetoed_candidate_never_reaches_the_llm(fake_client, tracker):
    """Gate order is a token-cost decision, so it is worth pinning."""
    validator = _CountingValidator()
    screener = _screener(fake_client, tracker, validator=validator, whale_veto_min_wallets=5)
    ctx = MarketContext(whale_coordination="SHORT", whale_wallet_count=6)
    candidate = _candidate("LONG")

    if not screener._whale_vetoed(candidate, ctx):
        screener._apply_validator(candidate, ctx)

    assert validator.calls == 0


def test_surviving_candidate_still_reaches_the_llm(fake_client, tracker):
    validator = _CountingValidator()
    screener = _screener(fake_client, tracker, validator=validator, whale_veto_min_wallets=5)
    ctx = MarketContext(whale_coordination="LONG", whale_wallet_count=6)
    candidate = _candidate("LONG")

    if not screener._whale_vetoed(candidate, ctx):
        screener._apply_validator(candidate, ctx)

    assert validator.calls == 1


# ── detectors stay pure ───────────────────────────────────────────────────
def test_detectors_do_not_read_the_new_context_fields():
    """Detectors stay pure functions of candles + context; the gate is orchestration."""
    import inspect

    from wolf.detectors import default_detectors

    for detector in default_detectors():
        source = inspect.getsource(type(detector)).lower()
        for banned in ("whale", "onchain_bias", "coinbase_premium"):
            assert banned not in source, f"{type(detector).__name__} must not read {banned}"


# ── debate context injection ──────────────────────────────────────────────
def test_onchain_lines_include_every_available_dimension():
    ctx = MarketContext(
        onchain_brief="VALUASI ON-CHAIN SOL — Fundamental mendukung LONG:\n  MCap $90,000M",
        onchain_bias="SUPPORTS_LONG",
        whale_coordination="LONG",
        whale_wallet_count=4,
        whale_long_count=6,
        whale_short_count=2,
        coinbase_premium_pct=0.12,
    )
    text = "\n".join(_onchain_lines(ctx))

    assert "VALUASI ON-CHAIN SOL" in text
    assert "6 long / 2 short" in text and "net 4 leaning LONG" in text
    assert "+0.120%" in text and "US institutions bidding" in text


def test_onchain_lines_fall_back_to_the_bias_without_a_brief():
    ctx = MarketContext(onchain_bias="SUPPORTS_SHORT")
    assert "On-chain valuation bias: SUPPORTS_SHORT" in "\n".join(_onchain_lines(ctx))


def test_onchain_lines_empty_without_data():
    assert _onchain_lines(MarketContext()) == []


def test_onchain_lines_read_the_premium_sign_correctly():
    assert "distributing" in _onchain_lines(MarketContext(coinbase_premium_pct=-0.2))[0]
    assert "no clear institutional bias" in _onchain_lines(MarketContext(coinbase_premium_pct=0.01))[0]


def test_describe_carries_onchain_context_to_the_debaters():
    ctx = MarketContext(
        funding_rate=-0.08,
        whale_coordination="LONG",
        whale_wallet_count=4,
        coinbase_premium_pct=0.12,
    )
    setup = _describe(_candidate("LONG"), ctx)

    assert "Funding rate: -0.0800%" in setup, "existing context still present"
    assert "Whale positioning" in setup
    assert "Coinbase premium" in setup


def test_describe_unchanged_when_no_onchain_data():
    setup = _describe(_candidate("LONG"), MarketContext(funding_rate=-0.08))
    assert "Whale positioning" not in setup
    assert "Coinbase premium" not in setup


def test_describe_tolerates_a_context_without_the_new_fields():
    class _Legacy:
        funding_rate = -0.05
        oi_change_pct = 3.0

    setup = _describe(_candidate("LONG"), _Legacy())
    assert "Funding rate" in setup


# ── the gate inside a real cycle ──────────────────────────────────────────
def test_run_cycle_drops_a_signal_the_whales_contradict(store, fake_client, tracker_settings):
    """Proves the gate is actually reached, not just callable in isolation."""
    from tests.test_screener import _breakout_candles
    from wolf.detectors.momentum import MomentumBreakoutDetector
    from wolf.tracker import Tracker

    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)

    class _OpposingContext:
        """A LONG breakout on a coin six whales just went short on."""

        def build(self, symbol):
            return MarketContext(whale_coordination="SHORT", whale_wallet_count=6)

    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None,
        universe=["BTCUSDT"], context_provider=_OpposingContext(),
        whale_veto_min_wallets=5,
    )

    assert screener.run_cycle() == []
    assert tracker.active_signals() == []


def test_run_cycle_keeps_the_signal_when_whales_agree(store, fake_client, tracker_settings):
    from tests.test_screener import _breakout_candles
    from wolf.detectors.momentum import MomentumBreakoutDetector
    from wolf.tracker import Tracker

    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)

    class _AgreeingContext:
        def build(self, symbol):
            return MarketContext(whale_coordination="LONG", whale_wallet_count=6)

    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None,
        universe=["BTCUSDT"], context_provider=_AgreeingContext(),
        whale_veto_min_wallets=5,
    )

    assert len(screener.run_cycle()) == 1
