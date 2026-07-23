"""Phase 3 verification — scoring engine (deterministic, network-free)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from anomaly.scoring import COMPONENT_MAX, MIN_ROWS, score_coin

TS = pd.date_range("2026-01-01", periods=90, freq="D", tz="UTC")


def _df(close, volume=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    if volume is None:
        volume = np.full(n, 1_000_000.0)
    return pd.DataFrame({
        "timestamp": TS[:n],
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": np.asarray(volume, dtype=float),
    })


def _meta(**over):
    m = {"id": "coin", "symbol": "coin", "name": "Coin",
         "market_cap": 2e8, "fdv": 3e8, "total_volume": 2e7, "in_dca_sleeve": False}
    m.update(over)
    return m


# ── bounds & shape ─────────────────────────────────────────────────────────
def test_score_shape_and_bounds():
    df = _df(100 + np.sin(np.linspace(0, 6, 90)))
    r = score_coin(df, _meta())
    assert set(r) == {"id", "symbol", "name", "in_dca_sleeve",
                      "score", "components", "flagged", "flags", "metrics"}
    assert 0 <= r["score"] <= 100
    for k, v in r["components"].items():
        assert 0.0 <= v <= COMPONENT_MAX[k]
    assert r["symbol"] == "COIN"


def test_total_equals_component_sum():
    df = _df(100 + np.cumsum(np.full(90, 0.1)))
    r = score_coin(df, _meta())
    assert r["score"] == max(0, min(100, round(sum(r["components"].values()))))


# ── component A: contraction ───────────────────────────────────────────────
def test_coiling_scores_higher_contraction_than_expanding():
    # Coiling: wide swings early, flat lately → low recent BB-width.
    early = 100 + 15 * np.sin(np.linspace(0, 8, 60))
    flat = 100 + 0.2 * np.sin(np.linspace(0, 6, 30))
    coil = _df(np.concatenate([early, flat]))
    # Expanding: flat early, wild lately.
    expand = _df(np.concatenate([flat, 100 + 15 * np.sin(np.linspace(0, 8, 60))]))
    a_coil = score_coin(coil, _meta())["components"]["volatility_contraction"]
    a_exp = score_coin(expand, _meta())["components"]["volatility_contraction"]
    assert a_coil > a_exp


# ── component B: volume anomaly ────────────────────────────────────────────
def test_volume_spike_scores_higher_than_flat_volume():
    close = 100 + 0.1 * np.arange(90)
    flat_v = np.full(90, 1_000_000.0)
    spike_v = flat_v.copy()
    spike_v[-3:] *= 4.0
    b_flat = score_coin(_df(close, flat_v), _meta())["components"]["volume_anomaly"]
    b_spike = score_coin(_df(close, spike_v), _meta())["components"]["volume_anomaly"]
    assert b_spike > b_flat
    assert b_flat < 5.0                       # quiet volume → near-zero anomaly


# ── component C: structure position ────────────────────────────────────────
def test_constructive_structure_beats_falling_knife():
    up = _df(100 + 0.3 * np.arange(90))              # steady uptrend, mid-upper range
    knife = _df(200 - 1.5 * np.arange(90))           # straight down, at range low
    c_up = score_coin(up, _meta())["components"]["structure_position"]
    c_knife = score_coin(knife, _meta())["components"]["structure_position"]
    assert c_up > c_knife


# ── component D: supply health + flags ─────────────────────────────────────
def test_healthy_supply_scores_higher_than_dilutive():
    df = _df(100 + 0.1 * np.arange(90))
    healthy = score_coin(df, _meta(market_cap=2e8, fdv=2.1e8))["components"]["supply_health"]
    dilutive = score_coin(df, _meta(market_cap=2e8, fdv=5.4e8))["components"]["supply_health"]
    assert healthy > dilutive


def test_high_fdv_mc_is_flagged():
    df = _df(100 + 0.1 * np.arange(90))
    r = score_coin(df, _meta(market_cap=5e7, fdv=3e8))   # FDV/MC = 6x
    assert r["flagged"] is True
    assert any("unlock_overhang" in f for f in r["flags"])


def test_wash_volume_is_flagged():
    df = _df(100 + 0.1 * np.arange(90))
    r = score_coin(df, _meta(market_cap=5e7, total_volume=2e8))  # turnover 4x
    assert r["flagged"] is True
    assert any("wash_volume" in f for f in r["flags"])


def test_healthy_coin_not_flagged():
    df = _df(100 + 0.1 * np.arange(90))
    r = score_coin(df, _meta(market_cap=2e8, fdv=2.4e8, total_volume=2e7))
    assert r["flagged"] is False
    assert r["flags"] == []


# ── guards ─────────────────────────────────────────────────────────────────
def test_insufficient_data_flagged():
    df = _df(100 + 0.1 * np.arange(MIN_ROWS - 5))
    r = score_coin(df, _meta())
    assert r["flagged"] is True
    assert r["flags"] == ["insufficient_data"]
    assert r["score"] == 0


def test_empty_frame_flagged():
    r = score_coin(pd.DataFrame(), _meta())
    assert r["flagged"] is True
    assert r["flags"] == ["insufficient_data"]


def test_dca_flag_passthrough():
    df = _df(100 + 0.1 * np.arange(90))
    r = score_coin(df, _meta(in_dca_sleeve=True))
    assert r["in_dca_sleeve"] is True
