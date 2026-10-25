"""Phase 3 — Scoring engine.

Turns a coin's ~90d OHLCV frame (Phase 2) plus its universe metadata (Phase 1)
into a deterministic 0-100 anomaly score for the coiling / pre-breakout swing
setup (the BANK / LAB / RAVE pattern). Four components, summed:

* **A — Volatility Contraction** (max 25): ATR(14)/ATR(60) — is short-term range
  compressing versus the longer baseline? Discrete tiers + a persistence bonus.
* **B — Volume Anomaly** (max 30): 24h volume vs its 30d average, but only when
  the last candle closes strong (upper 30% of its range) — that's what separates
  accumulation from a dump.
* **C — Structure Position** (max 25): where price sits in its 30d range —
  0.60-0.85 is "breakout imminent, not yet extended". Temporary stand-in for the
  planned derivative signal (Coinglass not wired yet).
* **D — Supply Health** (max 20): FDV/MC unlock pressure, minus a wash-trading
  turnover penalty.

**Red flags** (FDV/MC > 5, turnover > 5, market cap < $20M, < 60 OHLC rows) mean
the coin is auto-skipped: routed to a separate ``flagged`` list with no score.

``score_coin`` is pure (no network, no clock); every threshold is a module
constant so the rubric is easy to tune.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# ── red flags (auto-skip → `flagged`, never scored) ────────────────────────
MIN_ROWS = 60                 # need >= 60 candles; fewer → flagged
REDFLAG_FDV_MC = 5.0          # FDV/MC > this → severe unlock overhang
REDFLAG_TURNOVER = 5.0        # 24h volume > 5x mcap → wash-trading
REDFLAG_MIN_MCAP = 20_000_000

# ── component A: volatility contraction (max 25) ───────────────────────────
ATR_FAST = 14
ATR_SLOW = 60
A_RATIO_T1, A_PTS_1 = 0.50, 25    # ratio < 0.50 → 25
A_RATIO_T2, A_PTS_2 = 0.60, 18    # ratio < 0.60 → 18
A_RATIO_T3, A_PTS_3 = 0.70, 10    # ratio < 0.70 → 10
A_BONUS = 5                       # +5 if ratio < 0.70 sustained ≥ 14 days
A_BONUS_DAYS = 14
A_MAX = 25

# ── component B: volume anomaly (max 30) ───────────────────────────────────
VOL_BASELINE = 30                 # 30d average volume
B_RATIO_T1, B_PTS_1 = 5.0, 30     # vol_ratio > 5.0 → 30
B_RATIO_T2, B_PTS_2 = 3.0, 22     # vol_ratio > 3.0 → 22
B_RATIO_T3, B_PTS_3 = 2.0, 12     # vol_ratio > 2.0 → 12
B_CLOSE_STRENGTH = 0.70           # last close must be in upper 30% of its range
B_MAX = 30

# ── component C: structure position (max 25) ───────────────────────────────
STRUCT_WINDOW = 30
ATH_WINDOW = 90
C_MAX = 25

# ── component D: supply health (max 20) ────────────────────────────────────
D_FDV_T1, D_PTS_1 = 1.2, 20       # FDV/MC <= 1.2 → 20
D_FDV_T2, D_PTS_2 = 2.0, 12       # <= 2.0 → 12
D_FDV_T3, D_PTS_3 = 3.0, 5        # <= 3.0 → 5
D_FDV_UNKNOWN = 12                # missing FDV → neutral tier
D_WASH_TURNOVER = 3.0             # turnover > 3.0 → -15 penalty
D_WASH_PENALTY = 15
D_MAX = 20

COMPONENT_MAX = {
    "volatility_contraction": float(A_MAX),
    "volume_anomaly": float(B_MAX),
    "structure_position": float(C_MAX),
    "supply_health": float(D_MAX),
}


def score_coin(df: pd.DataFrame, coin_meta: dict) -> dict:
    """Score one coin, or red-flag it. Returns the breakdown + flag routing.

    Shape::

        {
          "id", "symbol", "name", "in_dca_sleeve",
          "score": int,                     # A+B+C+D, 0-100 (0 when flagged)
          "components": {A, B, C, D},
          "flagged": bool,                  # True → belongs in `flagged`, not `scored`
          "flags": [reasons],
          "metrics": {...raw signals for logging/inspection...},
        }
    """
    sym = str(coin_meta.get("symbol", "")).upper()
    base = {
        "id": coin_meta.get("id", coin_meta.get("coin_id", "")),
        "symbol": sym,
        "name": coin_meta.get("name", ""),
        "in_dca_sleeve": bool(coin_meta.get("in_dca_sleeve", False)),
    }

    mcap = _f(coin_meta.get("market_cap", coin_meta.get("mcap")))
    fdv = _f(coin_meta.get("fdv"))
    vol24 = _f(coin_meta.get("total_volume", coin_meta.get("volume_24h")))
    fdv_mc = (fdv / mcap) if (mcap > 0 and fdv > 0) else None
    turnover = (vol24 / mcap) if mcap > 0 else None
    n_rows = len(df) if (df is not None and "close" in getattr(df, "columns", [])) else 0

    # ── red flags → auto-skip (no scoring) ──
    flags = _red_flags(n_rows, mcap, fdv_mc, turnover)
    if flags:
        return {**base, "score": 0,
                "components": {k: 0.0 for k in COMPONENT_MAX},
                "flagged": True, "flags": flags,
                "metrics": {"fdv_mc": _r(fdv_mc), "turnover": _r(turnover),
                            "atr_ratio": None, "volume_ratio": None,
                            "range_position": None, "dist_from_ath_pct": None}}

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df.get("high", close), errors="coerce")
    low = pd.to_numeric(df.get("low", close), errors="coerce")
    volume = pd.to_numeric(df.get("volume", pd.Series(0.0, index=df.index)),
                           errors="coerce").fillna(0.0)

    a, a_m = _volatility_contraction(high, low, close)
    b, b_m = _volume_anomaly(volume, high, low, close)
    c, c_m = _structure_position(high, low, close)
    d, d_m = _supply_health(fdv_mc, turnover)

    components = {
        "volatility_contraction": float(a),
        "volume_anomaly": float(b),
        "structure_position": float(c),
        "supply_health": float(d),
    }
    total = int(max(0, min(100, a + b + c + d)))
    metrics = {**a_m, **b_m, **c_m, **d_m}
    return {**base, "score": total, "components": components,
            "flagged": False, "flags": [], "metrics": metrics}


# ── red flags ──────────────────────────────────────────────────────────────
def _red_flags(n_rows: int, mcap: float, fdv_mc: Optional[float],
               turnover: Optional[float]) -> list[str]:
    flags: list[str] = []
    if n_rows < MIN_ROWS:
        flags.append(f"insufficient_ohlc(<{MIN_ROWS})")
    if 0 < mcap < REDFLAG_MIN_MCAP:
        flags.append("micro_cap(<$20M)")
    if fdv_mc is not None and fdv_mc > REDFLAG_FDV_MC:
        flags.append(f"unlock_overhang(FDV/MC {fdv_mc:.1f}x)")
    if turnover is not None and turnover > REDFLAG_TURNOVER:
        flags.append(f"wash_volume(turnover {turnover:.1f}x)")
    return flags


# ── component A ─────────────────────────────────────────────────────────────
def _volatility_contraction(high: pd.Series, low: pd.Series,
                            close: pd.Series) -> tuple[float, dict]:
    """ATR(14)/ATR(60) contraction tiers + a >=14-day persistence bonus."""
    atr_fast = _atr_series(high, low, close, ATR_FAST)
    atr_slow = _atr_series(high, low, close, ATR_SLOW)
    ratio_s = (atr_fast / atr_slow.replace(0, np.nan)).dropna()

    if ratio_s.empty:
        return 0.0, {"atr_ratio": None}

    ratio = float(ratio_s.iloc[-1])
    if ratio < A_RATIO_T1:
        pts = A_PTS_1
    elif ratio < A_RATIO_T2:
        pts = A_PTS_2
    elif ratio < A_RATIO_T3:
        pts = A_PTS_3
    else:
        pts = 0

    # Bonus: contraction has held (ratio < 0.70) for >= 14 consecutive days.
    if len(ratio_s) >= A_BONUS_DAYS and (ratio_s.iloc[-A_BONUS_DAYS:] < A_RATIO_T3).all():
        pts += A_BONUS

    return float(min(pts, A_MAX)), {"atr_ratio": _r(ratio)}


# ── component B ─────────────────────────────────────────────────────────────
def _volume_anomaly(volume: pd.Series, high: pd.Series, low: pd.Series,
                    close: pd.Series) -> tuple[float, dict]:
    """24h vol vs 30d average — but only if the last candle closes strong."""
    last_vol = float(volume.iloc[-1])
    baseline = float(volume.iloc[-(VOL_BASELINE + 1):-1].mean()) if len(volume) > VOL_BASELINE else float(volume.iloc[:-1].mean())
    vol_ratio = (last_vol / baseline) if baseline > 0 else 0.0

    # Mandatory close-strength gate: accumulation, not dumping.
    rng = float(high.iloc[-1] - low.iloc[-1])
    close_pos = ((float(close.iloc[-1]) - float(low.iloc[-1])) / rng) if rng > 0 else 0.0

    if close_pos <= B_CLOSE_STRENGTH:
        pts = 0
    elif vol_ratio > B_RATIO_T1:
        pts = B_PTS_1
    elif vol_ratio > B_RATIO_T2:
        pts = B_PTS_2
    elif vol_ratio > B_RATIO_T3:
        pts = B_PTS_3
    else:
        pts = 0

    return float(pts), {"volume_ratio": _r(vol_ratio), "close_strength": _r(close_pos)}


# ── component C ─────────────────────────────────────────────────────────────
def _structure_position(high: pd.Series, low: pd.Series,
                        close: pd.Series) -> tuple[float, dict]:
    """Position within the 30d range; 0.60-0.85 = breakout imminent, not extended."""
    win = min(STRUCT_WINDOW, len(close))
    hi = float(high.iloc[-win:].max())
    lo = float(low.iloc[-win:].min())
    last = float(close.iloc[-1])
    rp = ((last - lo) / (hi - lo)) if hi > lo else 0.0

    if rp < 0.40:
        pts = 5                     # still basing
    elif rp < 0.60:
        pts = 15                    # mid-range
    elif rp <= 0.85:
        pts = 25                    # breakout imminent, not extended
    elif rp <= 0.90:
        pts = 10                    # getting extended (spec gap 0.85-0.90)
    else:
        pts = 0                     # > 0.90 → already extended, late

    ath_win = min(ATH_WINDOW, len(high))
    ath = float(high.iloc[-ath_win:].max())
    dist_from_ath = ((last - ath) / ath * 100.0) if ath > 0 else None

    return float(pts), {"range_position": _r(rp), "dist_from_ath_pct": _r(dist_from_ath)}


# ── component D ─────────────────────────────────────────────────────────────
def _supply_health(fdv_mc: Optional[float], turnover: Optional[float]) -> tuple[float, dict]:
    """FDV/MC unlock-pressure tiers, minus a wash-trading turnover penalty."""
    if fdv_mc is None:
        pts = D_FDV_UNKNOWN
    elif fdv_mc <= D_FDV_T1:
        pts = D_PTS_1
    elif fdv_mc <= D_FDV_T2:
        pts = D_PTS_2
    elif fdv_mc <= D_FDV_T3:
        pts = D_PTS_3
    else:
        pts = 0

    if turnover is not None and turnover > D_WASH_TURNOVER:
        pts -= D_WASH_PENALTY

    return float(max(0, min(D_MAX, pts))), {"fdv_mc": _r(fdv_mc), "turnover": _r(turnover)}


# ── indicators / helpers ────────────────────────────────────────────────────
def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    prev = prev.fillna(close)               # first bar: TR = high - low (no NaN)
    return pd.concat([
        high - low,
        (high - prev).abs(),
        (low - prev).abs(),
    ], axis=1).max(axis=1)


def _atr_series(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> pd.Series:
    """Simple-mean ATR over ``window`` (no talib — pure pandas)."""
    return _true_range(high, low, close).rolling(window).mean()


def _r(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(xf) or np.isinf(xf):
        return None
    return round(xf, 4)


def _f(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0
