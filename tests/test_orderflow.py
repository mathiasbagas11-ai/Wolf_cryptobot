"""Tests for the candle order-flow analytics (:mod:`wolf.orderflow`)."""

from __future__ import annotations

import math

from wolf import orderflow as flow
from wolf.detectors.base import score_flow
from wolf.models import Candle


def _c(v: float, buy: float = 0.5, trades: int = 100, o: float = 100.0, c: float = 100.0) -> Candle:
    return Candle(
        time=0, open=o, high=max(o, c) + 1, low=min(o, c) - 1, close=c,
        volume=v, trades=trades, taker_buy_volume=v * buy,
    )


def _series(n: int, v: float = 100.0, **kw) -> list[Candle]:
    return [_c(v, **kw) for _ in range(n)]


# ── volume_flow (FLOW) ───────────────────────────────────────────────────
def test_volume_flow_is_one_when_pace_is_unchanged():
    assert flow.volume_flow(_series(16, 100.0), fast=4, baseline=16) == 1.0


def test_volume_flow_scales_with_recent_pace():
    candles = _series(12, 100.0) + _series(4, 300.0)
    # Last 4 bars run at 3x the pace of the 16-bar window they belong to.
    assert flow.volume_flow(candles, fast=4, baseline=16) == 2.0


def test_volume_flow_needs_a_full_baseline():
    assert math.isnan(flow.volume_flow(_series(8), fast=4, baseline=16))


# ── trade_flow (S×) ──────────────────────────────────────────────────────
def test_trade_flow_tracks_counts_not_notional():
    """Many small fills lift S× while FLOW stays flat — GMGN's churn case."""
    candles = _series(12, 100.0, trades=100) + _series(4, 100.0, trades=400)
    assert flow.volume_flow(candles, 4, 16) == 1.0
    # Notional is flat while the trade count runs well above its own baseline.
    assert flow.trade_flow(candles, 4, 16) > 2.0


def test_trade_flow_unavailable_without_counts():
    candles = [
        Candle(time=0, open=100, high=101, low=99, close=100, volume=100.0)
        for _ in range(16)
    ]
    assert math.isnan(flow.trade_flow(candles, 4, 16))


# ── direction ────────────────────────────────────────────────────────────
def test_direction_needs_price_and_volume_to_agree():
    # Price up 5% and buyers clearly leading.
    assert flow.flow_direction(0.05, 0.70) == "BULLISH"
    # Price up but the tape is balanced -> not directional.
    assert flow.flow_direction(0.05, 0.50) == "MIXED"
    # Buyers leading but price flat -> not directional.
    assert flow.flow_direction(0.001, 0.80) == "MIXED"
    # Price down with sellers leading.
    assert flow.flow_direction(-0.05, 0.20) == "BEARISH"
    # Price down while buyers lead -> conflicting, so mixed.
    assert flow.flow_direction(-0.05, 0.80) == "MIXED"


def test_direction_unknown_without_taker_data():
    assert flow.flow_direction(0.05, float("nan")) == "UNKNOWN"


def test_dominance_margin_rejects_a_near_even_split():
    """A 51/49 split is noise, not a directional mandate."""
    assert flow.flow_direction(0.05, 0.51) == "MIXED"


# ── speed labels ─────────────────────────────────────────────────────────
def test_speed_buckets_match_the_radar():
    def speed(candles):
        return flow.analyse(candles, 4, 16).speed

    assert speed(_series(12, 100.0) + _series(4, 300.0)) == flow.HOT      # 2.00
    assert speed(_series(16, 100.0)) == flow.ACTIVE                       # 1.00
    assert speed(_series(12, 100.0) + _series(4, 50.0)) == flow.COOLING   # 0.57
    assert speed(_series(12, 100.0) + _series(4, 5.0)) == flow.COLD       # 0.07


def test_label_renders_the_gmgn_style_badge():
    candles = _series(12, 100.0, o=100, c=100) + [
        _c(300.0, buy=0.8, o=100, c=106) for _ in range(4)
    ]
    state = flow.analyse(candles, 4, 16)
    assert state.is_bullish and state.is_hot
    assert state.label().startswith("🔥📈")


# ── turnover (V/L) ───────────────────────────────────────────────────────
def test_turnover_is_volume_over_depth():
    assert flow.turnover(1_000_000, 50_000) == 20.0


def test_turnover_undefined_without_depth():
    assert math.isnan(flow.turnover(1_000_000, 0))


# ── the shared detector gate ─────────────────────────────────────────────
def _rally(buy: float, vol: float = 300.0) -> list[Candle]:
    return _series(12, 100.0, o=100, c=100) + [
        _c(vol, buy=buy, o=100 + i, c=103 + i) for i in range(4)
    ]


def _selloff(buy: float, vol: float = 300.0) -> list[Candle]:
    return _series(12, 100.0, o=100, c=100) + [
        _c(vol, buy=buy, o=100 - i, c=97 - i) for i in range(4)
    ]


def test_gate_rewards_flow_that_confirms_the_trade():
    verdict = score_flow(_rally(buy=0.8), is_long=True)
    assert verdict.points > 0
    assert not verdict.conflict
    assert "taker buys" in verdict.reason


def test_gate_flags_a_breakout_being_sold_into():
    """The 🔥📉 case: heavy volume that is distribution, not a breakout."""
    verdict = score_flow(_selloff(buy=0.15), is_long=True)
    assert verdict.points == 0
    assert verdict.conflict is True


def test_gate_is_neutral_on_balanced_flow():
    verdict = score_flow(_rally(buy=0.5), is_long=True)
    assert verdict.points == 0
    assert verdict.conflict is False


def test_gate_degrades_to_partial_credit_without_taker_data():
    """Venues with no aggressor flag score lower and can never veto."""
    candles = [
        Candle(time=0, open=100, high=101, low=99, close=100, volume=100.0)
        for _ in range(12
        )
    ] + [
        Candle(time=0, open=100 + i, high=105 + i, low=99 + i, close=104 + i, volume=300.0)
        for i in range(4)
    ]
    verdict = score_flow(candles, is_long=True, max_points=25)
    assert 0 < verdict.points < 25
    assert verdict.conflict is False
    assert "unverified" in verdict.reason

    # And the same data cannot veto a short either.
    assert score_flow(candles, is_long=False).conflict is False


# ── the gate as the detectors use it ─────────────────────────────────────
def test_breakout_sold_into_is_rejected_by_momentum():
    """A level taken out on aggressive selling is a trap, not a long.

    Direction-blind volume scoring cannot tell this from a real breakout —
    both print the same expansion — which is what the gate exists to fix.
    """
    from wolf.detectors import MomentumBreakoutDetector

    def bars(buy: float) -> list[Candle]:
        out = [
            Candle(time=i * 900_000, open=100, high=101, low=99, close=100,
                   volume=100.0, trades=100, taker_buy_volume=50.0)
            for i in range(60)
        ]
        # Breakout bar plus three more, all on heavy volume.
        for i in range(4):
            px = 102 + i * 2
            out.append(Candle(
                time=(60 + i) * 900_000, open=px - 1, high=px + 2, low=px - 2, close=px + 1,
                volume=600.0, trades=400, taker_buy_volume=600.0 * buy,
            ))
        return out

    det = MomentumBreakoutDetector()
    # Buyers lifting the offer: the setup survives the gate.
    assert det.evaluate("X", bars(buy=0.75)) is not None
    # Same price action, same volume, but sellers are the aggressors.
    assert det.evaluate("X", bars(buy=0.25)) is None


def test_flow_veto_can_be_switched_off():
    from wolf.detectors import MomentumBreakoutDetector

    out = [
        Candle(time=i * 900_000, open=100, high=101, low=99, close=100,
               volume=100.0, trades=100, taker_buy_volume=50.0)
        for i in range(60)
    ]
    for i in range(4):
        px = 102 + i * 2
        out.append(Candle(
            time=(60 + i) * 900_000, open=px - 1, high=px + 2, low=px - 2, close=px + 1,
            volume=600.0, trades=400, taker_buy_volume=150.0,
        ))
    assert MomentumBreakoutDetector(flow_veto=False).evaluate("X", out) is not None


def test_distribution_into_strength_is_a_conflict():
    """Price ticking up while sellers hit every bid is not a breakout.

    The strict price-and-volume reading calls this MIXED — price is up, so it
    is not "bearish" — which is why the gate judges the aggressor separately.
    """
    rally_sold_into = _rally(buy=0.25)
    state = flow.analyse(rally_sold_into, 4, 16)
    assert state.direction == "MIXED"          # price up, so not bearish
    assert state.aggressor_opposes(is_long=True)
    assert state.conflicts_with(is_long=True)


def test_balanced_tape_is_never_a_conflict():
    state = flow.analyse(_rally(buy=0.5), 4, 16)
    assert not state.conflicts_with(is_long=True)
    assert not state.conflicts_with(is_long=False)


def test_quiet_opposing_tape_is_not_a_conflict():
    """Without volume behind it, a lopsided split is noise, not distribution."""
    state = flow.analyse(_rally(buy=0.25, vol=5.0), 4, 16)
    assert not state.is_expanding
    assert not state.conflicts_with(is_long=True)
