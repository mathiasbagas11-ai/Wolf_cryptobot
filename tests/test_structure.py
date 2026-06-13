"""Tests for price-action structure helpers."""

from __future__ import annotations

from wolf import structure as st
from wolf.models import Candle


def _c(t, o, h, l, c, v=100.0):
    return Candle(time=t * 900_000, open=o, high=h, low=l, close=c, volume=v)


def test_swing_lows_and_highs():
    candles = [
        _c(0, 10, 11, 9, 10),
        _c(1, 10, 11, 8, 10),   # local low at index 1
        _c(2, 10, 13, 10, 12),  # local high at index 2
        _c(3, 12, 12, 10, 11),
        _c(4, 11, 12, 9, 10),
    ]
    assert 1 in st.swing_lows(candles, 1, 1)
    assert 2 in st.swing_highs(candles, 1, 1)


def test_bullish_liquidity_sweep():
    candles = [_c(i, 100, 101, 99, 100) for i in range(20)]
    # Last candle wicks below the prior 100-low (99) then closes back above it.
    candles.append(_c(20, 99.5, 101, 97, 100.5, 300.0))
    sweep = st.liquidity_sweep(candles, lookback=20)
    assert sweep.swept and sweep.sweep_type == "BULLISH_SWEEP"
    assert sweep.recovery > 0


def test_bearish_liquidity_sweep():
    candles = [_c(i, 100, 101, 99, 100) for i in range(20)]
    candles.append(_c(20, 100.5, 103, 99, 99.5, 300.0))
    sweep = st.liquidity_sweep(candles, lookback=20)
    assert sweep.swept and sweep.sweep_type == "BEARISH_SWEEP"


def test_no_sweep_in_range():
    candles = [_c(i, 100, 101, 99, 100) for i in range(25)]
    assert st.liquidity_sweep(candles, lookback=20).swept is False


def test_insufficient_data_no_sweep():
    assert st.liquidity_sweep([_c(0, 1, 1, 1, 1)], lookback=20).swept is False
