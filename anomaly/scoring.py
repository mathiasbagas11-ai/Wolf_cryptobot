"""Phase 3 — Scoring engine.

Turns a coin's 90d OHLCV frame (Phase 2) plus its universe metadata (Phase 1)
into a deterministic 0-100 anomaly score for the coiling / pre-breakout swing
setup (the BANK / LAB / RAVE pattern). Four components:

* **A — Volatility Contraction** (max 25): is price coiling? Bollinger-band
  squeeze + range contraction.
* **B — Volume Anomaly** (max 30): unusual volume vs its own baseline — the
  footprint of accumulation before a move.
* **C — Structure Position** (max 25): where price sits in its range + trend
  posture. This is a *temporary stand-in* for the planned derivative signal
  (funding / OI / liquidations) — Coinglass isn't wired up yet, so we read
  structure straight off price for now.
* **D — Supply Health** (max 20): tokenomics sanity from FDV/MC and turnover.
  A hard red flag here (severe unlock overhang, wash-trading volume, or
  unusable data) routes the coin to a separate ``flagged`` list instead of
  being scored.

``score_coin`` is a pure function of its inputs (no network, no clock) so it
unit-tests and back-tests deterministically. All thresholds live as module
constants up top so the rubric is easy to tune.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

# ── general ────────────────────────────────────────────────────────────────
MIN_ROWS = 30                 # need at least ~30 candles to measure contraction

# ── component A: volatility contraction ────────────────────────────────────
BB_WINDOW = 20                # Bollinger period
SQUEEZE_MAX = 15.0            # sub-budget: BB-width percentile
SQUEEZE_PCTILE_TIGHT = 0.10   # ≤ this percentile → tightest → full squeeze credit
SQUEEZE_PCTILE_LOOSE = 0.70   # ≥ this percentile → no squeeze credit
RANGE_MAX = 10.0             # sub-budget: recent vs prior range contraction
RANGE_WINDOW = 14
CONTRACT_TIGHT = 0.55        # recent/prior range ≤ this → full credit
CONTRACT_LOOSE = 1.00        # ≥ this → expanding → no credit

# ── component B: volume anomaly ────────────────────────────────────────────
VOL_RECENT = 5               # days in the "recent" volume window
VOL_BASELINE = 30            # trailing baseline window
VOL_RATIO_MAX = 18.0         # sub-budget: recent/baseline volume expansion
VOL_RATIO_LO = 0.9           # ratio ≤ this → no credit (volume quiet/dead)
VOL_RATIO_HI = 2.5           # ratio ≥ this → full expansion credit
VOL_Z_MAX = 12.0             # sub-budget: latest-bar volume z-score
VOL_Z_FULL = 2.5             # z ≥ this → full credit

# ── component C: structure position (derivative stand-in) ──────────────────
STRUCT_WINDOW = 30           # Donchian range window
POS_MAX = 15.0               # sub-budget: position within range
POS_IDEAL = 0.60             # coiling constructively under resistance
POS_WIDTH = 0.26             # gaussian tolerance around the ideal
TREND_MAX = 10.0             # sub-budget: trend posture vs a longer MA
TREND_WINDOW = 50

# ── component D: supply health ─────────────────────────────────────────────
FDV_SCORE_MAX = 12.0
TURNOVER_SCORE_MAX = 8.0
FDV_MC_FLAG = 3.0            # FDV/MC ≥ this → severe unlock overhang → FLAG
TURNOVER_WASH_FLAG = 3.0    # 24h volume ≥ 3× mcap → wash-trading → FLAG
FDV_MC_HEALTHY = 1.0        # FDV/MC at/below this → no unlock pressure → full
TURNOVER_LO = 0.05          # healthy turnover band (liquidity sufficiency)
TURNOVER_HI = 1.50

COMPONENT_MAX = {
    "volatility_contraction": 25.0,
    "volume_anomaly": 30.0,
    "structure_position": 25.0,
    "supply_health": 20.0,
}


def score_coin(df: pd.DataFrame, coin_meta: dict) -> dict:
    """Score one coin. Returns the breakdown, total, and flag routing.

    Shape::

        {
          "id", "symbol", "name", "in_dca_sleeve",
          "score": int,                     # A+B+C+D, 0-100
          "components": {A, B, C, D},       # per-component points
          "flagged": bool,                  # True → belongs in `flagged`, not `scored`
          "flags": [reasons],
          "metrics": {...raw signals for logging/inspection...},
        }

    A ``flagged`` coin still carries a computed score (informative), but the
    caller routes it to the ``flagged`` list rather than ``scored``.
    """
    sym = str(coin_meta.get("symbol", "")).upper()
    base = {
        "id": coin_meta.get("id", ""),
        "symbol": sym,
        "name": coin_meta.get("name", ""),
        "in_dca_sleeve": bool(coin_meta.get("in_dca_sleeve", False)),
    }

    if df is None or len(df) < MIN_ROWS or "close" not in df:
        return {**base, "score": 0,
                "components": {k: 0.0 for k in COMPONENT_MAX},
                "flagged": True, "flags": ["insufficient_data"], "metrics": {}}

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df.get("high", close), errors="coerce")
    low = pd.to_numeric(df.get("low", close), errors="coerce")
    volume = pd.to_numeric(df.get("volume", pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0)

    a, a_m = _volatility_contraction(close)
    b, b_m = _volume_anomaly(volume)
    c, c_m = _structure_position(close, high, low)
    d, d_flags, d_m = _supply_health(coin_meta)

    components = {
        "volatility_contraction": round(a, 1),
        "volume_anomaly": round(b, 1),
        "structure_position": round(c, 1),
        "supply_health": round(d, 1),
    }
    total = int(round(a + b + c + d))
    total = max(0, min(100, total))

    metrics = {**a_m, **b_m, **c_m, **d_m}
    return {**base, "score": total, "components": components,
            "flagged": bool(d_flags), "flags": d_flags, "metrics": metrics}


# ── component A ─────────────────────────────────────────────────────────────
def _volatility_contraction(close: pd.Series) -> tuple[float, dict]:
    """BB-width squeeze percentile + recent-vs-prior range contraction."""
    sma = close.rolling(BB_WINDOW).mean()
    std = close.rolling(BB_WINDOW).std()
    bbw = (4.0 * std) / sma.replace(0, np.nan)          # (upper-lower)/mid = 4σ/mean
    bbw = bbw.dropna()

    squeeze, pctile = 0.0, None
    if len(bbw) >= 2:
        current = float(bbw.iloc[-1])
        pctile = float((bbw <= current).mean())          # 0 = tightest ever
        squeeze = _lin(pctile, SQUEEZE_PCTILE_LOOSE, SQUEEZE_PCTILE_TIGHT) * SQUEEZE_MAX

    contraction, ratio = 0.0, None
    if len(close) >= 2 * RANGE_WINDOW:
        recent = close.iloc[-RANGE_WINDOW:]
        prior = close.iloc[-2 * RANGE_WINDOW:-RANGE_WINDOW]
        recent_rng = float(recent.max() - recent.min())
        prior_rng = float(prior.max() - prior.min())
        if prior_rng > 0:
            ratio = recent_rng / prior_rng
            contraction = _lin(ratio, CONTRACT_LOOSE, CONTRACT_TIGHT) * RANGE_MAX

    score = _clamp(squeeze + contraction, 0.0, COMPONENT_MAX["volatility_contraction"])
    return score, {"bbw_percentile": _r(pctile), "range_contraction_ratio": _r(ratio)}


# ── component B ─────────────────────────────────────────────────────────────
def _volume_anomaly(volume: pd.Series) -> tuple[float, dict]:
    """Recent/baseline volume expansion + latest-bar z-score."""
    vol = volume[volume >= 0]
    if len(vol) < VOL_RECENT + 5:
        return 0.0, {"volume_ratio": None, "volume_z": None}

    baseline_win = vol.iloc[-VOL_BASELINE:] if len(vol) >= VOL_BASELINE else vol
    baseline = float(baseline_win.mean())
    recent = float(vol.iloc[-VOL_RECENT:].mean())

    ratio_score, ratio = 0.0, None
    if baseline > 0:
        ratio = recent / baseline
        ratio_score = _lin(ratio, VOL_RATIO_LO, VOL_RATIO_HI) * VOL_RATIO_MAX

    z_score_pts, z = 0.0, None
    std = float(baseline_win.std())
    if std > 0:
        z = (float(vol.iloc[-1]) - baseline) / std
        z_score_pts = _clamp(z / VOL_Z_FULL, 0.0, 1.0) * VOL_Z_MAX

    score = _clamp(ratio_score + z_score_pts, 0.0, COMPONENT_MAX["volume_anomaly"])
    return score, {"volume_ratio": _r(ratio), "volume_z": _r(z)}


# ── component C (derivative stand-in) ───────────────────────────────────────
def _structure_position(close: pd.Series, high: pd.Series, low: pd.Series) -> tuple[float, dict]:
    """Position within the Donchian range (gaussian around the ideal) + trend."""
    win = min(STRUCT_WINDOW, len(close))
    hi = float(high.iloc[-win:].max())
    lo = float(low.iloc[-win:].min())
    last = float(close.iloc[-1])

    pos, pos_score = None, 0.0
    if hi > lo:
        pos = (last - lo) / (hi - lo)
        pos_score = POS_MAX * math.exp(-(((pos - POS_IDEAL) / POS_WIDTH) ** 2))

    tw = min(TREND_WINDOW, len(close))
    sma = close.rolling(tw).mean()
    trend_score = 0.0
    above = rising = None
    if sma.notna().sum() >= 6:
        sma_last = float(sma.iloc[-1])
        above = last > sma_last
        rising = float(sma.iloc[-1] - sma.iloc[-6]) > 0
        if above and rising:
            trend_score = TREND_MAX
        elif above:
            trend_score = TREND_MAX * 0.6
        elif rising:
            trend_score = TREND_MAX * 0.4
        else:
            trend_score = TREND_MAX * 0.2

    score = _clamp(pos_score + trend_score, 0.0, COMPONENT_MAX["structure_position"])
    return score, {"range_position": _r(pos), "above_ma": above, "ma_rising": rising}


# ── component D ─────────────────────────────────────────────────────────────
def _supply_health(coin_meta: dict) -> tuple[float, list[str], dict]:
    """FDV/MC + turnover health. Hard problems become red flags (→ `flagged`)."""
    mcap = _f(coin_meta.get("market_cap"))
    fdv = _f(coin_meta.get("fdv"))
    vol24 = _f(coin_meta.get("total_volume"))

    fdv_mc = fdv / mcap if (mcap > 0 and fdv > 0) else None
    turnover = vol24 / mcap if mcap > 0 else None

    flags: list[str] = []
    if fdv_mc is not None and fdv_mc >= FDV_MC_FLAG:
        flags.append(f"unlock_overhang(FDV/MC {fdv_mc:.1f}x)")
    if turnover is not None and turnover >= TURNOVER_WASH_FLAG:
        flags.append(f"wash_volume(turnover {turnover:.1f}x)")

    # FDV/MC: 1.0 (fully circulating) → full credit; ≥ flag threshold → none.
    if fdv_mc is None:
        fdv_score = FDV_SCORE_MAX * 0.5        # unknown supply → neutral
    else:
        fdv_score = _lin(fdv_mc, FDV_MC_FLAG, FDV_MC_HEALTHY) * FDV_SCORE_MAX

    # Turnover: reward the healthy liquidity band, taper outside it.
    if turnover is None:
        turnover_score = 0.0
    elif TURNOVER_LO <= turnover <= TURNOVER_HI:
        turnover_score = TURNOVER_SCORE_MAX
    elif turnover < TURNOVER_LO:
        turnover_score = TURNOVER_SCORE_MAX * _clamp(turnover / TURNOVER_LO, 0.0, 1.0)
    else:  # above healthy band but below wash flag
        turnover_score = TURNOVER_SCORE_MAX * _lin(turnover, TURNOVER_WASH_FLAG, TURNOVER_HI)

    score = _clamp(fdv_score + turnover_score, 0.0, COMPONENT_MAX["supply_health"])
    return score, flags, {"fdv_mc": _r(fdv_mc), "turnover": _r(turnover)}


# ── helpers ─────────────────────────────────────────────────────────────────
def _lin(x: float, zero_at: float, full_at: float) -> float:
    """Linear ramp → [0,1]. ``full_at`` maps to 1, ``zero_at`` to 0 (either order)."""
    if full_at == zero_at:
        return 0.0
    t = (x - zero_at) / (full_at - zero_at)
    return _clamp(t, 0.0, 1.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _r(x: Optional[float]) -> Optional[float]:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), 4)


def _f(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0
