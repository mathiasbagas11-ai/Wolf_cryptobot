"""Phase 4 verification — entry ladder (deterministic, network-free)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from anomaly.entry import (
    INVALIDATION_MULT,
    TP1_ATR_MULT,
    TP2_ATR_MULT,
    build_ladder,
    format_price,
)

TS = pd.date_range("2026-01-01", periods=90, freq="D", tz="UTC")


def _df(close, high=None, low=None, volume=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    high = close * 1.02 if high is None else np.asarray(high, dtype=float)
    low = close * 0.98 if low is None else np.asarray(low, dtype=float)
    volume = np.full(n, 1_000_000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({"timestamp": TS[:n], "open": close, "high": high,
                         "low": low, "close": close, "volume": volume})


def test_ladder_strictly_descending_and_below_price():
    df = _df(100 + 2 * np.sin(np.linspace(0, 10, 90)))
    cp = float(df["close"].iloc[-1])
    lad = build_ladder(df, cp)
    assert lad["l1"] > lad["l2"] > lad["l3"]
    assert lad["l1"] < cp
    assert lad["l3"] > lad["invalidation"]     # invalidation is below the lowest rung


def test_tp_and_invalidation_formulas():
    df = _df(100 + 0.1 * np.arange(90))
    cp = float(df["close"].iloc[-1])
    lad = build_ladder(df, cp)
    atr = lad["atr14"]
    swing_low = float(df["low"].iloc[-90:].min())
    assert lad["tp1"] == cp + atr * TP1_ATR_MULT
    assert lad["tp2"] == cp + atr * TP2_ATR_MULT
    assert lad["invalidation"] == swing_low * INVALIDATION_MULT
    assert lad["tp2"] > lad["tp1"] > cp


def test_l1_clamped_when_support_above_price():
    # Force support_30d above current price so raw L1 would exceed it.
    close = np.full(90, 100.0)
    low = np.full(90, 100.0)          # 30d low == 100
    df = _df(close, low=low)
    cp = 99.0                          # current price below the support floor
    lad = build_ladder(df, cp)
    assert lad["l1"] < cp
    assert lad["l1"] == cp * 0.995
    assert lad["l1"] > lad["l2"] > lad["l3"]


def test_rr_ratio_positive_and_reasonable():
    df = _df(100 + 3 * np.sin(np.linspace(0, 8, 90)))
    cp = float(df["close"].iloc[-1])
    lad = build_ladder(df, cp)
    assert lad["rr_ratio"] > 0
    # RR = (tp1 - avg_entry) / (avg_entry - invalidation)
    expected = round((lad["tp1"] - lad["avg_entry"]) / (lad["avg_entry"] - lad["invalidation"]), 2)
    assert lad["rr_ratio"] == expected


def test_sizes_present_and_sum_correctly():
    df = _df(100 + 0.1 * np.arange(90))
    s = build_ladder(df, 100.0)["sizes"]
    assert s["l1"] + s["l2"] + s["l3"] == 1.0
    assert s["tp1"] + s["tp2"] + s["runner"] == 1.0
    assert s["trailing_stop_pct"] == 0.20


def test_degenerate_inputs_return_empty_plan():
    lad = build_ladder(_df(100 + 0.1 * np.arange(90)), 0.0)
    assert lad["l1"] is None and lad["rr_ratio"] == 0.0
    lad2 = build_ladder(pd.DataFrame({"close": [1.0]}), 1.0)
    assert lad2["l1"] is None


def test_format_price_tiers():
    assert format_price(0.0034567) == "$0.003457"     # < $0.01 → 6dp
    assert format_price(0.5432) == "$0.5432"          # $0.01-$1 → 4dp
    assert format_price(1.0) == "$1.0000"             # boundary ($1) → 4dp
    assert format_price(1234.5678) == "$1,234.57"     # > $1 → 2dp
    assert format_price(None) == "—"
    assert format_price(float("nan")) == "—"


def test_inverted_formula_gets_reordered():
    # Downtrend where VWAP (90d avg) sits well above current price → raw L2 high.
    close = np.linspace(200, 100, 90)
    df = _df(close)
    cp = float(close[-1])              # 100, far below the 90d VWAP (~150)
    lad = build_ladder(df, cp)
    assert lad["l1"] > lad["l2"] > lad["l3"]
    assert lad["l1"] < cp             # top rung still clamped below price
