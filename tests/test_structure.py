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


# ── Order Blocks ──────────────────────────────────────────────────────────
def test_bullish_ob_detected():
    """Bearish candle before 3-candle bullish impulse → bullish OB."""
    # Base + OB candle (bearish) + 3-candle impulse up + padding
    candles = [_c(i, 100, 101, 99, 100) for i in range(30)]
    candles.append(_c(30, 103, 104, 101, 101.5))  # bearish OB candle (close < open)
    candles.append(_c(31, 101.5, 105, 101, 104))   # impulse candle 1
    candles.append(_c(32, 104, 108, 103, 107))      # impulse candle 2
    candles.append(_c(33, 107, 111, 106, 110))      # impulse candle 3
    for i in range(34, 65):
        candles.append(_c(i, 110, 111, 109, 110))
    blocks = st.find_order_blocks(candles, lookback=50)
    bull_obs = [b for b in blocks if b.kind == "BULL"]
    assert len(bull_obs) >= 1
    ob = bull_obs[-1]
    assert ob.top >= ob.bottom
    assert ob.top == max(103, 101.5)   # max(open, close) of OB candle
    assert ob.bottom == min(103, 101.5)


def test_bearish_ob_detected():
    """Bullish candle before 3-candle bearish impulse → bearish OB."""
    candles = [_c(i, 100, 101, 99, 100) for i in range(30)]
    candles.append(_c(30, 97, 100, 96, 98.5))   # bullish OB candle (close > open)
    candles.append(_c(31, 98.5, 99, 95, 96))     # impulse candle 1
    candles.append(_c(32, 96, 97, 92, 93))        # impulse candle 2
    candles.append(_c(33, 93, 94, 89, 90))        # impulse candle 3
    for i in range(34, 65):
        candles.append(_c(i, 90, 91, 89, 90))
    blocks = st.find_order_blocks(candles, lookback=50)
    bear_obs = [b for b in blocks if b.kind == "BEAR"]
    assert len(bear_obs) >= 1
    ob = bear_obs[-1]
    assert ob.top == max(97, 98.5)
    assert ob.bottom == min(97, 98.5)


def test_price_in_ob():
    blocks = [st.OrderBlock("BULL", top=105.0, bottom=103.0)]
    assert st.price_in_ob(104.0, blocks, "BULL") is True
    assert st.price_in_ob(102.0, blocks, "BULL") is False
    assert st.price_in_ob(104.0, blocks, "BEAR") is False


def test_no_ob_on_flat_market():
    """Flat doji candles produce no impulse → no order blocks."""
    candles = [_c(i, 100, 101, 99, 100) for i in range(65)]
    assert st.find_order_blocks(candles, lookback=50) == []


# ── Structure Break (BOS / ChoCh) ─────────────────────────────────────────
def test_bullish_bos_detected():
    """Price closing above a confirmed swing high → BOS detected."""
    # Build enough candles so len >= lookback + 5 = 30 (guard in find_structure_break).
    # After the pivot high we use strictly declining candles so no subsequent candle
    # can form a higher swing high, making the pivot the most-recent confirmed high.
    candles = []
    # Preamble: 8 candles outside the 25-candle window (will be sliced off)
    for i in range(8):
        candles.append(_c(i, 100, 101, 99, 100))
    # Rising approach: 6 candles (window indices 0-5)
    for i in range(6):
        base = 100 + i * 0.4
        candles.append(_c(8 + i, base, base + 0.4, base - 0.3, base + 0.1))
    # Pivot high (window index 6): high sticks out clearly above all neighbours
    p_pivot = 104.0
    pivot_high = p_pivot + 4  # 108.0
    candles.append(_c(14, p_pivot, pivot_high, p_pivot - 0.2, p_pivot + 2.5))
    # Declining candles after pivot (window indices 7-20): strictly lower highs
    # so no new swing high can form above the pivot
    for j in range(14):
        base = p_pivot + 2.3 - j * 0.25
        candles.append(_c(15 + j, base, base + 0.15, base - 0.3, base - 0.1))
    # Breakout candle (window index 21): close above pivot_high
    candles.append(_c(29, p_pivot, pivot_high + 2, p_pivot - 0.2, pivot_high + 1.5))
    # Two more trailing candles so window has last 25 and breakout isn't the very end
    candles.append(_c(30, pivot_high + 1, pivot_high + 1.5, p_pivot - 0.1, pivot_high + 1.2))
    candles.append(_c(31, pivot_high + 1.2, pivot_high + 1.8, pivot_high, pivot_high + 1.5))

    sb = st.find_structure_break(candles, lookback=25)
    assert sb is not None
    assert sb.direction == "BULLISH"
    assert sb.kind in ("BOS", "CHOCH")
    assert sb.broken_level == pivot_high


def test_no_bos_within_range():
    """No structure break when the latest candle stays within prior range."""
    candles = [_c(i, 100, 102, 98, 100) for i in range(50)]
    assert st.find_structure_break(candles, lookback=40) is None
