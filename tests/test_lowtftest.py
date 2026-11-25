"""Tests for /trendtest 1h|15m: v2's rule transplanted bar for bar.

The point of the test is the transplant, so these check it is v2's rule (the
same functions and bar counts, not a re-tuned copy), that the halves split
at the median trade date, that the shifted baseline does not credit random
walks, and that the plain /trendtest still runs the original backtest.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from wolf import lowtftest, trendtest
from wolf.bbtest import DEFAULT_SYMBOLS
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter

T0 = 1704067200000


def _basket(seed, n=5000, tf="15m", vol=0.0035, rho=0.7, k=4):
    bar_ms = lowtftest.FRAMES[tf][0]
    rng = random.Random(seed)
    common = [[rng.gauss(0, 1) for _ in range(4)] for _ in range(n)]
    out = {}
    for sym in DEFAULT_SYMBOLS[:k]:
        p, rows = 100.0, []
        for i in range(n):
            path = [p]
            for s in range(4):
                z = math.sqrt(rho) * common[i][s] + math.sqrt(1 - rho) * rng.gauss(0, 1)
                path.append(path[-1] * math.exp(z * vol / 2))
            rows.append(Candle(time=T0 + i * bar_ms, open=path[0], high=max(path), low=min(path),
                               close=path[-1], volume=1.0))
            p = path[-1]
        out[sym] = rows
    return out


def test_the_transplant_trades_exactly_what_v2s_own_rule_trades_on_the_same_bars():
    series = _basket(3, tf="1h", vol=0.007)
    report = lowtftest.backtest("1h", series=series, cost_bps=20.0, draws=10)
    regime = trendtest.btc_regime(series["BTCUSDT"])
    direct = sum(len(trendtest.run_trend(c, regime)) for c in series.values())
    assert report["trend"]["n"] == direct > 0


def test_the_card_states_v2s_bar_counts_and_not_new_ones():
    text = lowtftest.render(lowtftest.backtest("15m", series=_basket(4), cost_bps=20.0, draws=10))
    assert f"{trendtest.REGIME_BARS}-bar mean" in text
    assert f"{trendtest.BREAKOUT_BARS}-bar breakout" in text
    assert f"out after {trendtest.HOLD_BARS} bars" in text


def test_the_halves_split_at_the_median_trade_date():
    a = lowtftest.backtest("15m", series=_basket(5), cost_bps=20.0, draws=10)["trend"]
    assert abs(a["first"]["n"] - a["second"]["n"]) <= 1
    assert a["first"]["n"] + a["second"]["n"] == a["n"]


def test_cost_in_r_is_charged_on_each_trades_own_stop():
    a = lowtftest.backtest("15m", series=_basket(6), cost_bps=20.0, draws=10)["trend"]
    assert abs(a["gross"] - a["cost"] - a["net"]) < 0.002


def test_on_correlated_random_walks_the_rule_does_not_pass():
    for seed in (11, 12):
        a = lowtftest.backtest("15m", series=_basket(seed), cost_bps=0.0, draws=60)["trend"]
        assert a["beats"] < lowtftest.BEAT_LINE
        assert not a["verdict"].startswith("PASSES")


def test_an_unknown_timeframe_is_refused_by_name():
    assert "timeframe must be one of" in lowtftest.backtest("5m", series=_basket(7))["error"]


def test_without_btc_there_is_no_regime_and_the_card_says_so():
    s = _basket(8)
    del s["BTCUSDT"]
    assert "no BTC history" in lowtftest.render(lowtftest.backtest("15m", series=s))


def _router(monkeypatch):
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    return CommandRouter(app)


def test_trendtest_with_a_timeframe_runs_the_transplant_and_plain_trendtest_is_unchanged(monkeypatch):
    seen = []
    monkeypatch.setattr(lowtftest, "backtest", lambda tf, client=None, cost_bps=20.0: seen.append(tf) or {
        "tf": tf, "cost_bps": cost_bps, "error": "stub"})
    monkeypatch.setattr(trendtest, "backtest", lambda symbols=None, client=None, cost_bps=20.0: seen.append("4h") or {
        "cost_bps": cost_bps, "error": "stub"})
    router = _router(monkeypatch)
    assert "V2-ON-15M" in router.handle("/trendtest 15m")
    assert "V2-ON-1H" in router.handle("/trendtest 1h")
    assert "TREND-TEST" in router.handle("/trendtest")
    assert "V2-ON-1D SPOT LONG-ONLY" in router.handle("/trendtest 1d")
    assert "Pakai" in router.handle("/trendtest 5m")
    assert seen == ["15m", "1h", "4h", "1d"]


# ── 1d: spot, long only, only in BTC's UP regime ─────────────────────────

def _daily(seed, n=1500, drift=0.0):
    rng = random.Random(seed)
    out = {}
    for sym in DEFAULT_SYMBOLS[:4]:
        p, rows = 100.0, []
        for i in range(n):
            o = p
            p = p * math.exp(drift + rng.gauss(0, 0.03))
            rows.append(Candle(time=T0 + i * 86_400_000, open=o, high=max(o, p) * 1.01,
                               low=min(o, p) * 0.99, close=p, volume=1.0))
        out[sym] = rows
    return out


def test_on_1d_spot_no_short_is_ever_taken_even_when_btc_falls():
    a = lowtftest.backtest("1d", series=_daily(21, drift=-0.002), cost_bps=20.0, draws=10)["trend"]
    assert not a.get("n") or a["short"]["n"] == 0


def test_on_1d_spot_every_trade_is_a_long_taken_while_btc_is_above_its_mean():
    series = _daily(22, drift=0.001)
    a = lowtftest.backtest("1d", series=series, cost_bps=20.0, draws=10)["trend"]
    regime = trendtest.btc_regime(series["BTCUSDT"])
    longs = sum(1 for c in series.values() for t in trendtest.run_trend(c, regime) if t["long"])
    assert a["n"] > 0 and a["short"]["n"] == 0 and a["long"]["n"] <= longs


def test_the_1d_read_uses_the_stricter_fourth_look_bar_and_says_spot():
    report = lowtftest.backtest("1d", series=_daily(23, drift=0.001), cost_bps=20.0, draws=10)
    assert report["beat_line"] == 98.33
    text = lowtftest.render(report)
    assert "SPOT LONG-ONLY" in text and "needs 98.33%" in text and "no funding" in text


def test_the_lower_timeframes_keep_their_registered_bar():
    assert lowtftest.BEAT_BY_TF["1h"] == lowtftest.BEAT_BY_TF["15m"] == 97.5
