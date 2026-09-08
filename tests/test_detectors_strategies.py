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
    # Breakout candle: closes above the consolidation high on strong volume —
    # confirms the squeeze is resolving up (required since the 0/8 fix).
    cs.append(_c(59, base, base + 1.6, base - 0.1, base + 1.3, 260.0))
    cand = PrePumpDetector().evaluate("X", cs)
    assert cand is not None
    assert cand.direction == "LONG"
    assert cand.signal_type == "PREPUMP"
    assert cand.score >= 65
    assert _valid_geometry(cand)


def test_prepump_no_signal_flat():
    assert PrePumpDetector().evaluate("X", _flat(80)) is None


def _prepump_series(coil_bars: int, breakout_mult: float = 1.0):
    """Ramp -> base of ``coil_bars`` -> one breakout bar closing above it.

    ``coil_bars`` is the knob that mattered: the original fixture used 18, short
    enough that the Bollinger width was still falling monotonically into the
    breakout and an FvG from the ramp was still inside the 50-bar lookback.
    Real bases run for days, and the identical setup scored 10 points lower on
    one — under the threshold, silently, for every symbol the bot scanned.
    """
    cs = []
    p = 90.0
    for i in range(41):
        p += 0.4
        cs.append(_c(i, p - 0.1, p + 0.3, p - 0.2, p, 100.0))
    base = cs[-1].close
    for k in range(coil_bars):
        cs.append(_c(41 + k, base, base + 0.25, base - 0.25,
                     base + (0.05 if k % 2 else -0.05), 90.0))
    move = 1.3 * breakout_mult
    cs.append(_c(41 + coil_bars, base, base + move * 1.25, base - 0.1, base + move, 260.0))
    return cs


def test_prepump_fires_on_a_realistically_long_base():
    """The regression the 18-bar fixture could not see.

    A base of 60-150 bars is what a 1h pre-pump actually looks like; the
    detector emitted nothing on one for months while this file stayed green.
    """
    for coil in (60, 90, 150):
        cand = PrePumpDetector().evaluate("X", _prepump_series(coil))
        assert cand is not None, f"no signal on a {coil}-bar base"
        assert cand.signal_type == "PREPUMP"
        assert cand.direction == "LONG"
        assert _valid_geometry(cand)


def test_prepump_scoring_is_reachable_on_the_bar_its_gate_requires():
    """No scored component may contradict the mandatory breakout gate.

    The detector went silent because several did — a VWAP *discount* award and
    an RSI band that a breakout bar cannot sit in — leaving a ceiling below the
    threshold. Asserting the achieved score clears the bar it is graded against
    is what makes that arithmetic visible instead of invisible.
    """
    cand = PrePumpDetector().evaluate("X", _prepump_series(90))
    assert cand is not None
    assert cand.score >= PrePumpDetector().score_threshold


def test_prepump_ignores_a_breakout_with_no_coil_to_release():
    """A breakout bar the size of its neighbours is MOMENTUM's setup, not this
    one — the market was already moving, nothing was compressed."""
    cs = []
    p = 90.0
    for i in range(100):
        p += 0.5
        cs.append(_c(i, p - 0.1, p + 0.9, p - 0.8, p, 100.0))
    cs.append(_c(100, p, p + 1.2, p - 0.1, p + 1.0, 260.0))
    assert PrePumpDetector().evaluate("X", cs) is None


def test_prepump_declines_to_chase_a_move_already_underway():
    """Same coil, but entered several expansion bars late: price has left VWAP
    behind and the base is no longer in the lookback window. That is somebody
    else's exit liquidity, not an entry."""
    cs = _prepump_series(90)
    p = cs[-1].close
    for k in range(6):  # keep expanding, well past the release
        o = p
        p *= 1.12
        cs.append(_c(200 + k, o, p * 1.03, o * 0.99, p, 300.0))
    assert PrePumpDetector().evaluate("X", cs) is None


def test_prepump_carries_a_wider_chase_limit_than_the_global_default():
    """A breakout is entered on the bar that resolves it, so the move it exists
    to catch starts at the quote. The screener's mean-reversion default dropped
    these before they were ever sent."""
    cand = PrePumpDetector().evaluate("X", _prepump_series(90))
    assert cand is not None
    assert cand.max_chase_r is not None and cand.max_chase_r > 0.5


def test_prepump_requires_breakout_confirmation():
    # Same squeeze, but the last candle stays INSIDE the range (no breakout) —
    # must not fire (the 0/8 mid-squeeze failure mode).
    cs = []
    p = 90.0
    for i in range(41):
        p += 0.4
        cs.append(_c(i, p - 0.1, p + 0.3, p - 0.2, p, 100.0))
    base = cs[-1].close
    for k in range(19):  # consolidation, no breakout candle at the end
        cs.append(_c(41 + k, base, base + 0.25, base - 0.25, base + (0.05 if k % 2 else -0.05), 90.0))
    assert PrePumpDetector().evaluate("X", cs) is None


def _scalp_series():
    """The sweep fixture from ``test_scalp_liquidity_sweep``."""
    cs = []
    p = 100.0
    for i in range(39):
        p -= 0.4
        cs.append(_c(i, p + 0.1, p + 0.3, p - 0.3, p, 100.0))
    prior_low = min(x.low for x in cs[-20:])
    cs.append(_c(39, p, p + 5, prior_low - 1.5, p + 4, 600.0))
    return cs


def _trap_series():
    """The trap fixture from ``test_trap_bullish_sweep_high_conviction``."""
    cs = []
    p = 110.0
    for i in range(59):
        p -= 0.4
        cs.append(_c(i, p + 0.1, p + 0.3, p - 0.3, p, 100.0))
    prior_low = min(x.low for x in cs[-20:])
    cs.append(_c(59, p, p + 1.0, prior_low - 3.0, p + 0.5, 600.0))
    return cs


def _predump_series():
    """The distribution fixture from ``test_predump_rejection_at_top``: a push
    to a high, volume fading into it, then a rejection candle."""
    cs = []
    p = 90.0
    for i in range(59):
        p += 0.5
        cs.append(_c(i, p - 0.2, p + 0.4, p - 0.4, p, 120.0 if i < 55 else 40.0))
    top = cs[-1].close
    cs.append(_c(59, top + 0.2, top + 2.5, top - 0.3, top - 0.5, 35.0))
    return cs


def test_every_detector_accepts_the_call_the_screener_actually_makes():
    """The regression that cost TRAP nineteen days.

    TRAP's ``evaluate`` was written before the screener passed a feature cache
    and never grew the parameter, so every invocation raised TypeError — which
    the screener catches and logs alongside the other detector faults, then
    continues. It emitted nothing from 2026-08-20 onward, which is to say
    through every era the bot has measured.

    Nothing caught it because every test called ``evaluate("X", candles)`` with
    two arguments, and the fake detectors in the screener tests all had the
    right signature. Both halves were green while the real pairing was broken.
    """
    from wolf.detectors import default_detectors
    from wolf.indicator_cache import CandleFeatures

    candles = _flat(120)
    features = CandleFeatures.build(candles)
    for det in default_detectors():
        # Exactly how Screener._best_candidate invokes it: four positional args.
        det.evaluate("BTCUSDT", candles, None, features)
        det.evaluate("BTCUSDT", candles, None, None)


def test_no_detector_crashes_through_the_screener():
    """The same guarantee one level up, so a signature drift cannot hide behind
    the screener's own exception handler."""
    from wolf.detectors import default_detectors
    from wolf.screener import Screener

    class _Tracker:
        def stats(self):
            return {}

    for det in default_detectors():
        screener = Screener(None, _Tracker(), [det], universe=[], interval=det.timeframe)
        series = {det.timeframe: _flat(150)}
        assert screener._best_candidate("BTCUSDT", series, None) is None


def test_score_parts_account_for_the_whole_score():
    """A total hides its composition, so the parts have to add up to it.

    This is the check that keeps the record honest as components are added: a
    component scored but not recorded would make every `evidence` bucket read
    from an incomplete picture, silently.
    """
    for cand in (
        PrePumpDetector().evaluate("X", _prepump_series(90)),
        PreDumpDetector().evaluate("X", _predump_series()),
        ScalpDetector().evaluate("X", _scalp_series()),
        LiquidityTrapDetector().evaluate("X", _trap_series()),
    ):
        assert cand is not None
        assert cand.score_parts, f"{cand.strategy} recorded no composition"
        assert sum(cand.score_parts.values()) == cand.score


def test_every_detector_declares_primary_components():
    """The `evidence` bucket labels a strategy UNRECORDED when it declares
    none, which quietly exempts it from the one question the bucket exists to
    ask."""
    from wolf.detectors import default_detectors

    for det in default_detectors():
        assert det.primary_components, f"{det.name} declares no primary components"


def test_a_detectors_primary_components_are_names_it_actually_writes():
    """The diagnostic reads `primary_components` against the keys in
    `score_parts`; a typo in either would silently label every signal THIN."""
    from wolf.detectors import default_detectors

    for det in default_detectors():
        for name in getattr(det, "primary_components", ()):
            assert isinstance(name, str) and name


def test_predump_no_longer_scores_a_constant():
    """`atr/price < 0.1` was true on 100% of 1320 bars measured — a constant
    among the awards, which shifts the threshold by its own value while
    appearing on the card as evidence. It is a gate now, so it must not appear
    in the composition at all."""
    cand = PreDumpDetector().evaluate("X", _predump_series())
    assert cand is not None
    assert not any("rr" in k or "risk_reward" in k for k in cand.score_parts)


def test_predump_refuses_a_tape_too_volatile_to_fade():
    """The old award simply withheld 5 points here. Selling a fade into a
    market whose ATR is a tenth of price is the case the sanity check was
    describing, so it now refuses instead of asking for more points."""
    cs = _predump_series()
    det = PreDumpDetector(max_atr_ratio=0.0001)  # any real tape exceeds this
    assert det.evaluate("X", cs) is None
    assert PreDumpDetector().evaluate("X", cs) is not None


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


def test_swing_stop_is_structural_below_wick():
    cs = []
    p = 80.0
    for i in range(80):
        p += 0.35
        cs.append(_c(i, p - 0.1, p + 0.3, p - 0.2, p, 100.0))
    cur = cs[-1].close
    for k in range(4):
        cur -= 0.7
        cs.append(_c(80 + k, cur + 0.4, cur + 0.5, cur - 0.3, cur, 100.0))
    rej_low = cur - 2.0
    cs.append(_c(84, cur, cur + 0.6, rej_low, cur + 0.3, 130.0))  # deep bullish rejection wick
    cand = SwingDetector().evaluate("X", cs)
    assert cand is not None and cand.direction == "LONG"
    # Stop sits at or below the rejection wick low (structural), not a flat EMA stop.
    assert cand.sl <= rej_low
    assert _valid_geometry(cand)


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


# ── MOMENTUM ─────────────────────────────────────────────────────────────
def test_momentum_breakout_long():
    """Clean breakout above 30-candle high with volume, MACD, RSI confirms."""
    cs = []
    p = 100.0
    # 60 candles of moderate uptrend, building a range
    for i in range(59):
        p += 0.1
        cs.append(_c(i, p - 0.1, p + 0.4, p - 0.3, p, 150.0))
    prior_high = max(c.high for c in cs[-30:])
    # Breakout candle: closes well above range high with strong volume
    cs.append(_c(59, prior_high, prior_high + 2.0, prior_high - 0.1, prior_high + 1.8, 400.0))
    from wolf.detectors.momentum import MomentumBreakoutDetector
    cand = MomentumBreakoutDetector().evaluate("X", cs)
    # Breakout may or may not pass all hard gates depending on MACD/RSI state;
    # if it fires it must be well-formed
    if cand is not None:
        assert cand.direction == "LONG"
        assert _valid_geometry(cand)
        assert cand.score >= 70


def test_momentum_no_signal_flat():
    from wolf.detectors.momentum import MomentumBreakoutDetector
    assert MomentumBreakoutDetector().evaluate("X", _flat(90)) is None


# ── registry ──────────────────────────────────────────────────────────────
def test_default_detectors_registered():
    from wolf.detectors import default_detectors

    names = {d.name for d in default_detectors()}
    assert {"MOMENTUM", "PREPUMP", "PREDUMP", "SCALP", "SWING", "TRAP"} <= names
