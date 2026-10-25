"""Phase 4 — Entry ladder.

Turns a coin's OHLCV frame + current price into a concrete *paper* trade plan:
three scaled limit-buy rungs (a pullback ladder), an invalidation level, two
take-profit targets, a trailing runner, and the resulting risk/reward ratio.

Everything is anchored to price structure — ATR for spacing, 30d support / 90d
VWAP / 90d swing-low for the rung floors — so the plan degrades sensibly across
regimes. :func:`build_ladder` is a pure function (no network, no clock).

Sizing (fixed): L1 40% · L2 35% · L3 25%; take-profit TP1 sells 30%, TP2 sells
30%, the 40% runner rides a 20% trailing stop.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# ── ladder geometry ────────────────────────────────────────────────────────
ATR_WINDOW = 14
SUPPORT_WINDOW = 30
VWAP_WINDOW = 90
SWING_WINDOW = 90

L1_ATR_MULT = 0.5
L2_ATR_MULT = 1.5
L3_FLOOR_MULT = 1.02          # swing_low * 1.02
L3_PRICE_MULT = 0.80          # …or price * 0.80, whichever is higher
L1_MAX_PRICE_MULT = 0.995     # top rung must sit just below current price

TP1_ATR_MULT = 3.0
TP2_ATR_MULT = 6.0
INVALIDATION_MULT = 0.95      # swing_low * 0.95

# entry fill sizes (fractions) and exit plan
ENTRY_SIZES = (0.40, 0.35, 0.25)     # L1, L2, L3
TP_SIZES = (0.30, 0.30)              # TP1, TP2
RUNNER_SIZE = 0.40
TRAILING_STOP_PCT = 0.20


def build_ladder(df: pd.DataFrame, current_price: float) -> dict:
    """Build the scaled entry ladder + exits for one coin.

    Returns a dict with the required keys ``l1, l2, l3, invalidation, tp1, tp2,
    rr_ratio`` plus rendering helpers (``avg_entry``, ``atr14``, ``sizes``). RR
    is measured full-ladder: (TP1 − weighted-avg entry) / (avg entry − invalidation).
    Returns an empty-ish plan (levels ``None``) when inputs are unusable.
    """
    if df is None or len(df) < 2 or current_price is None or current_price <= 0:
        return _empty()

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df.get("high", close), errors="coerce")
    low = pd.to_numeric(df.get("low", close), errors="coerce")
    volume = pd.to_numeric(df.get("volume", pd.Series(0.0, index=df.index)),
                           errors="coerce").fillna(0.0)

    atr14 = _atr(high, low, close, ATR_WINDOW)
    support_30d = float(low.iloc[-SUPPORT_WINDOW:].min())
    swing_low = float(low.iloc[-SWING_WINDOW:].min())
    vwap_90d = _vwap(close.iloc[-VWAP_WINDOW:], volume.iloc[-VWAP_WINDOW:])

    l1 = max(current_price - atr14 * L1_ATR_MULT, support_30d)
    l2 = max(current_price - atr14 * L2_ATR_MULT, vwap_90d)
    l3 = max(swing_low * L3_FLOOR_MULT, current_price * L3_PRICE_MULT)

    l1, l2, l3 = _order_ladder(l1, l2, l3, current_price)

    invalidation = swing_low * INVALIDATION_MULT
    tp1 = current_price + atr14 * TP1_ATR_MULT
    tp2 = current_price + atr14 * TP2_ATR_MULT

    avg_entry = l1 * ENTRY_SIZES[0] + l2 * ENTRY_SIZES[1] + l3 * ENTRY_SIZES[2]
    rr_ratio = _rr(avg_entry, tp1, invalidation)

    return {
        "l1": l1, "l2": l2, "l3": l3,
        "invalidation": invalidation,
        "tp1": tp1, "tp2": tp2,
        "rr_ratio": rr_ratio,
        # rendering / planning extras
        "avg_entry": avg_entry,
        "atr14": atr14,
        "sizes": {
            "l1": ENTRY_SIZES[0], "l2": ENTRY_SIZES[1], "l3": ENTRY_SIZES[2],
            "tp1": TP_SIZES[0], "tp2": TP_SIZES[1],
            "runner": RUNNER_SIZE, "trailing_stop_pct": TRAILING_STOP_PCT,
        },
    }


# ── ladder ordering ────────────────────────────────────────────────────────
def _order_ladder(l1: float, l2: float, l3: float, current_price: float) -> tuple[float, float, float]:
    """Guarantee a strictly-descending ladder that sits below current price.

    Sort the rungs descending (the formulas can invert them), clamp the top
    rung below price, then nudge any tied/inverted lower rung down so the
    invariant ``l1 > l2 > l3`` always holds.
    """
    l1, l2, l3 = sorted((l1, l2, l3), reverse=True)
    if l1 >= current_price:
        l1 = current_price * L1_MAX_PRICE_MULT
    if l2 >= l1:
        l2 = l1 * 0.99
    if l3 >= l2:
        l3 = l2 * 0.99
    return l1, l2, l3


# ── indicators ─────────────────────────────────────────────────────────────
def _atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> float:
    """Average True Range (simple mean of True Range over ``window``)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr = tr.dropna()
    if tr.empty:
        return 0.0
    win = min(window, len(tr))
    return float(tr.iloc[-win:].mean())


def _vwap(close: pd.Series, volume: pd.Series) -> float:
    """Volume-weighted average price; falls back to mean close if no volume."""
    vol_sum = float(volume.sum())
    if vol_sum <= 0:
        return float(close.mean())
    return float((close * volume).sum() / vol_sum)


def _rr(avg_entry: float, tp1: float, invalidation: float) -> float:
    risk = avg_entry - invalidation
    if risk <= 0:
        return 0.0
    return round((tp1 - avg_entry) / risk, 2)


def _empty() -> dict:
    return {
        "l1": None, "l2": None, "l3": None,
        "invalidation": None, "tp1": None, "tp2": None,
        "rr_ratio": 0.0, "avg_entry": None, "atr14": 0.0,
        "sizes": {
            "l1": ENTRY_SIZES[0], "l2": ENTRY_SIZES[1], "l3": ENTRY_SIZES[2],
            "tp1": TP_SIZES[0], "tp2": TP_SIZES[1],
            "runner": RUNNER_SIZE, "trailing_stop_pct": TRAILING_STOP_PCT,
        },
    }


# ── price formatting ───────────────────────────────────────────────────────
def format_price(price: Optional[float]) -> str:
    """Tiered decimals: < $0.01 → 6dp, $0.01-$1 → 4dp, > $1 → 2dp."""
    if price is None or (isinstance(price, float) and (np.isnan(price) or np.isinf(price))):
        return "—"
    p = float(price)
    ap = abs(p)
    if ap < 0.01:
        return f"${p:.6f}"
    if ap <= 1:
        return f"${p:.4f}"
    return f"${p:,.2f}"
