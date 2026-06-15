"""Trigger tests for the ported strategy detectors.

Each test builds a deterministic candle scenario that exercises the detector's
core trigger, then asserts the candidate is well-formed (direction, score above
threshold, and TP/SL on the correct side of entry). A flat-market control
confirms no false positives.
"""

from __future__ import annotations

from wolf.detectors import (
    LiquidityTrapDetector,
    PreDumpDetector,
    PrePumpDetector,
    ScalpDetector,
    SwingDetector,
)
from wolf.models import Candle


def _c(t, o, h, l, c, v=100.0):
    return Candle(time=t * 900_000, open=o, high=h, low=l, close=c, volume=v)


def _flat(n=90):
    return [_c(i, 100, 101, 99, 100, 100.0) for i in range(n)]


def _valid_geometry(cand) -> bool:
    if cand.direction == "LONG":
        return cand.tp > cand.entry_price > cand.sl
    return cand.tp < cand.entry_price < cand.sl


# ── SCALP ────────────────────────────────────────────────────────────────
def test_scalp_bullish_sweep():
    cs = []
    p = 110.0
    for i in range(39):
        p -= 0.4
        cs.append(_c(i, p + 0.1, p + 0.3, p - 0.3, p, 100.0))
    prior_low = min(x.low for x in cs[-20:])
    cs.append(_c(39, p, p + 5, prior_low - 1.5, p + 4, 600.0))  # sweep + reclaim + volume
    cand = ScalpDetector().evaluate("X", cs)
    assert cand is not None
    assert cand.direction == "LONG"
    assert cand.signal_type == "SCALP"
    assert cand.score >= 60
    assert _valid_geometry(cand)


def test_scalp_no_signal_flat():
    assert ScalpDetector().evaluate("X", _flat(40)) is None


# ── PREPUMP ──────────────────────────────────────────────────────────────
def test_prepump_squeeze_then_coil():
    cs = []
    p = 90.0
    for i in range(41):
        p += 0.4
        cs.append(_c(i, p - 0.1, p + 0.3, p - 0.2, p, 100.0))
    base = cs[-1].close
    for k in range(18):  # tight consolidation -> Bollinger squeeze
        cs.append(_c(41 + k, base, base + 0.25, base - 0.25, base + (0.05 if k % 2 else -0.05), 90.0))
    cs.append(_c(59, base, base + 0.3, base - 0.2, base + 0.1, 250.0))  # volume coil release
    cand = PrePumpDetector().evaluate("X", cs)
    assert cand is not None
    assert cand.direction == "LONG"
    assert cand.signal_type == "PREPUMP"
    assert cand.score >= 65
    assert _valid_geometry(cand)


def test_prepump_no_signal_flat():
    assert PrePumpDetector().evaluate("X", _flat(80)) is None


# ── PREDUMP ──────────────────────────────────────────────────────────────
def test_predump_rejection_at_top():
    cs = []
    p = 90.0
    for i in range(59):
        p += 0.5
        cs.append(_c(i, p - 0.2, p + 0.4, p - 0.4, p, 120.0 if i < 55 else 40.0))
    top = cs[-1].close
    cs.append(_c(59, top + 0.2, top + 2.5, top - 0.3, top - 0.5, 35.0))  # rejection, fading volume
    cand = PreDumpDetector().evaluate("X", cs)
    assert cand is not None
    assert cand.direction == "SHORT"
    assert cand.signal_type == "PREDUMP"
    assert cand.score >= 65
    assert _valid_geometry(cand)


def test_predump_no_signal_flat():
    assert PreDumpDetector().evaluate("X", _flat(80)) is None


# ── SWING ────────────────────────────────────────────────────────────────
def test_swing_pullback_in_uptrend():
    cs = []
    p = 80.0
    for i in range(80):
        p += 0.35
        cs.append(_c(i, p - 0.1, p + 0.3, p - 0.2, p, 100.0))
    cur = cs[-1].close
    for k in range(4):  # pullback toward EMA20
        cur -= 0.7
        cs.append(_c(80 + k, cur + 0.4, cur + 0.5, cur - 0.3, cur, 100.0))
    cs.append(_c(84, cur, cur + 0.6, cur - 2.0, cur + 0.3, 130.0))  # bullish rejection
    cand = SwingDetector().evaluate("X", cs)
    assert cand is not None
    assert cand.direction == "LONG"
    assert cand.signal_type == "SWING"
    assert cand.entry_mode == "RETEST_WAIT"
    assert _valid_geometry(cand)


def test_swing_no_signal_flat():
    assert SwingDetector().evaluate("X", _flat(90)) is None


# ── TRAP (liquidity-trap reversal, high conviction) ────────────────────────
def test_trap_bullish_sweep_high_conviction():
    cs = []
    p = 110.0
    for i in range(59):  # steady downtrend → low RSI, declining lows
        p -= 0.4
        cs.append(_c(i, p + 0.1, p + 0.3, p - 0.3, p, 100.0))
    prior_low = min(x.low for x in cs[-20:])
    # Deep sweep below the range, blow-off volume, strong reclaim with a
    # dominant lower wick — the trap springs.
    cs.append(_c(59, p, p + 1.0, prior_low - 3.0, p + 0.5, 600.0))
    cand = LiquidityTrapDetector().evaluate("X", cs)
    assert cand is not None
    assert cand.direction == "LONG"
    assert cand.signal_type == "TRAP"
    assert cand.confluence_level == "HIGH"
    assert cand.score >= 80  # high conviction only
    assert _valid_geometry(cand)


def test_trap_ignores_shallow_reclaim():
    """A sweep that barely reclaims (weak recovery) is not a sprung trap."""
    cs = []
    p = 110.0
    for i in range(59):
        p -= 0.4
        cs.append(_c(i, p + 0.1, p + 0.3, p - 0.3, p, 100.0))
    prior_low = min(x.low for x in cs[-20:])
    # Pierces the low but closes near the bottom → recovery below the gate.
    cs.append(_c(59, p, p + 0.2, prior_low - 3.0, prior_low - 2.5, 600.0))
    assert LiquidityTrapDetector().evaluate("X", cs) is None


def test_trap_no_signal_flat():
    assert LiquidityTrapDetector().evaluate("X", _flat(80)) is None


# ── registry ──────────────────────────────────────────────────────────────
def test_default_detectors_registered():
    from wolf.detectors import default_detectors

    names = {d.name for d in default_detectors()}
    assert {"MOMENTUM", "PREPUMP", "PREDUMP", "SCALP", "SWING", "TRAP"} <= names
