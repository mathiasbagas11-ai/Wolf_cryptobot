"""Tests for on-chain context recorded on signals, and its outcome buckets.

None of this data gates anything except the whale veto. It is recorded so that
in a few weeks the question "is any of it predictive?" can be answered from
numbers instead of from a hunch — the same measure-then-enable path the regime
and AI flags already follow.
"""

from __future__ import annotations

from wolf.diagnose import diagnose, render_digest
from wolf.market import MarketContext
from wolf.models import Signal, Status
from wolf.screener import _onchain_annotations


# ── MarketContext.whale_stance ────────────────────────────────────────────
def test_whale_stance_is_relative_to_the_trade_direction():
    """Stored relative, because "whales were LONG" means opposite things for a
    LONG and a SHORT signal — bucketing on the raw side averages the effect away."""
    whales_long = MarketContext(whale_coordination="LONG", whale_wallet_count=5)

    assert whales_long.whale_stance("LONG") == "WITH"
    assert whales_long.whale_stance("SHORT") == "AGAINST"


def test_whale_stance_empty_without_data():
    """No data is a different fact from a level book, and stays distinguishable."""
    assert MarketContext().whale_stance("LONG") == ""
    assert MarketContext(whale_coordination="LONG").whale_stance("") == ""


# ── annotations captured at record time ───────────────────────────────────
def test_annotations_snapshot_every_dimension():
    ctx = MarketContext(
        onchain_bias="SUPPORTS_SHORT",
        whale_coordination="SHORT",
        whale_wallet_count=4,
        coinbase_premium_pct=0.12,
    )
    annotations = _onchain_annotations(ctx, "LONG")

    assert annotations == {
        "onchain_bias": "SUPPORTS_SHORT",
        "whale_stance": "AGAINST",
        "whale_net_wallets": 4,
        "coinbase_premium_pct": 0.12,
    }


def test_annotations_empty_without_a_context():
    assert _onchain_annotations(None, "LONG") == {}


def test_annotations_degrade_when_collectors_have_not_run():
    annotations = _onchain_annotations(MarketContext(), "LONG")

    assert annotations["onchain_bias"] == ""
    assert annotations["whale_stance"] == ""
    assert annotations["coinbase_premium_pct"] is None


def test_annotations_tolerate_a_context_without_the_new_fields():
    class _Legacy:
        funding_rate = -0.05

    assert _onchain_annotations(_Legacy(), "LONG") == {
        "onchain_bias": "", "whale_stance": "",
        "whale_net_wallets": 0, "coinbase_premium_pct": None,
    }


# ── signals carry the annotations through the tracker ─────────────────────
def test_recorded_signal_carries_onchain_context(store, fake_client, tracker_settings):
    from wolf.tracker import Tracker

    tracker = Tracker(store, fake_client, tracker_settings)
    signal = tracker.record_signal(
        symbol="SOLUSDT", signal_type="MOMENTUM", direction="LONG",
        entry_price=100.0, tp=110.0, sl=95.0, strategy="MOMENTUM",
        onchain_bias="SUPPORTS_LONG", whale_stance="WITH",
        whale_net_wallets=6, coinbase_premium_pct=0.12,
    )

    assert signal.onchain_bias == "SUPPORTS_LONG"
    assert signal.whale_stance == "WITH"
    assert signal.whale_net_wallets == 6
    assert signal.coinbase_premium_pct == 0.12


def test_signal_defaults_are_empty_not_wrong(store, fake_client, tracker_settings):
    from wolf.tracker import Tracker

    tracker = Tracker(store, fake_client, tracker_settings)
    signal = tracker.record_signal(
        symbol="SOLUSDT", signal_type="MOMENTUM", direction="LONG",
        entry_price=100.0, tp=110.0, sl=95.0, strategy="MOMENTUM",
    )

    assert signal.onchain_bias == ""
    assert signal.whale_stance == ""
    assert signal.coinbase_premium_pct is None


# ── outcome buckets ───────────────────────────────────────────────────────
def _outcome(status: str, r: float, **annotations) -> Signal:
    return Signal(
        symbol="SOLUSDT", signal_type="MOMENTUM", direction="LONG",
        entry_price=100.0, tp=110.0, sl=95.0, strategy="MOMENTUM",
        status=status, r_multiple=r, pnl_pct=r * 5.0, exit_price=100.0 + r * 5.0,
        **annotations,
    )


class _StubTracker:
    def __init__(self, outcomes: list[Signal]) -> None:
        self._outcomes = outcomes

    def outcomes(self) -> list[Signal]:
        return list(self._outcomes)


def _diag(outcomes: list[Signal]) -> dict:
    return diagnose(_StubTracker(outcomes))


def test_buckets_split_signals_by_whale_stance():
    outcomes = [
        _outcome(Status.TP_HIT.value, 2.0, whale_stance="WITH"),
        _outcome(Status.TP_HIT.value, 2.0, whale_stance="WITH"),
        _outcome(Status.SL_HIT.value, -1.0, whale_stance="AGAINST"),
        _outcome(Status.SL_HIT.value, -1.0, whale_stance="AGAINST"),
    ]
    buckets = _diag(outcomes)["by_whale_stance"]

    assert buckets["WITH"]["n"] == 2 and buckets["WITH"]["mean_r"] == 2.0
    assert buckets["AGAINST"]["n"] == 2 and buckets["AGAINST"]["mean_r"] == -1.0


def test_no_data_is_its_own_bucket_not_folded_into_neutral():
    """A collector that was off is not the same finding as one that saw nothing."""
    outcomes = [
        _outcome(Status.TP_HIT.value, 1.0, onchain_bias="NEUTRAL"),
        _outcome(Status.SL_HIT.value, -1.0),                       # collector was off
    ]
    buckets = _diag(outcomes)["by_onchain_bias"]

    assert set(buckets) == {"NEUTRAL", "NO_DATA"}
    assert buckets["NO_DATA"]["n"] == 1


def test_buckets_report_win_rate_and_verdict():
    outcomes = [_outcome(Status.TP_HIT.value, 2.0, whale_stance="WITH") for _ in range(3)]
    outcomes.append(_outcome(Status.SL_HIT.value, -1.0, whale_stance="WITH"))

    bucket = _diag(outcomes)["by_whale_stance"]["WITH"]

    assert bucket["graded"] == 4
    assert bucket["win_rate"] == 75.0
    assert "verdict" in bucket


def test_buckets_are_empty_without_outcomes():
    assert _diag([])["by_whale_stance"] == {}


# ── digest rendering ──────────────────────────────────────────────────────
def test_digest_prints_onchain_buckets_once_data_exists():
    outcomes = [
        _outcome(Status.TP_HIT.value, 2.0, whale_stance="WITH", onchain_bias="SUPPORTS_LONG"),
        _outcome(Status.SL_HIT.value, -1.0, whale_stance="AGAINST", onchain_bias="SUPPORTS_SHORT"),
    ]
    digest = render_digest(_diag(outcomes))

    assert "whale:WITH" in digest and "whale:AGAINST" in digest
    assert "onchain:SUPPORTS_LONG" in digest


def test_digest_stays_quiet_when_no_collector_ran():
    """A deployment with the collectors off must not carry NO_DATA lines forever."""
    digest = render_digest(_diag([_outcome(Status.TP_HIT.value, 2.0)]))

    assert "whale:" not in digest
    assert "onchain:" not in digest
