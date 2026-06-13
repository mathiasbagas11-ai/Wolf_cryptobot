"""Tests for the momentum breakout detector."""

from __future__ import annotations

from wolf.detectors import MomentumBreakoutDetector
from wolf.models import Candle


def _flat_then_breakout(direction: str) -> list[Candle]:
    candles = []
    # 60 flat candles around 100 with modest volume.
    for i in range(60):
        candles.append(Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0))
    # Strong breakout candle with volume spike.
    if direction == "LONG":
        candles.append(Candle(time=60 * 900_000, open=100, high=108, low=100, close=107, volume=500.0))
    else:
        candles.append(Candle(time=60 * 900_000, open=100, high=100, low=92, close=93, volume=500.0))
    return candles


def test_detects_long_breakout():
    det = MomentumBreakoutDetector()
    cand = det.evaluate("BTCUSDT", _flat_then_breakout("LONG"))
    assert cand is not None
    assert cand.direction == "LONG"
    assert cand.tp > cand.entry_price > cand.sl
    assert cand.score >= 65


def test_detects_short_breakdown():
    det = MomentumBreakoutDetector()
    cand = det.evaluate("BTCUSDT", _flat_then_breakout("SHORT"))
    assert cand is not None
    assert cand.direction == "SHORT"
    assert cand.tp < cand.entry_price < cand.sl


def test_no_signal_when_flat():
    det = MomentumBreakoutDetector()
    flat = [Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0) for i in range(80)]
    assert det.evaluate("BTCUSDT", flat) is None


def test_insufficient_candles():
    det = MomentumBreakoutDetector()
    assert det.evaluate("BTCUSDT", []) is None
