"""Tests for /sweeptest, the trend-pullback sweep from a Threads screenshot.

They pin down causality: higher-timeframe trend from complete bars only, a
swing used only after it is confirmed, the entry on the next open, the
target at least 1R. The shifted baseline must not credit random walks.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from wolf import sweeptest
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter
from wolf.sweeptest import H1_MS, backtest, render, run_rule, trend_by_bar

T0 = 1704067200000


def _basket(seed, n=8000, vol=0.008, rho=0.7):
    rng = random.Random(seed)
    common = [[rng.gauss(0, 1) for _ in range(6)] for _ in range(n)]
    out = {}
    for sym in sweeptest.DEFAULT_SYMBOLS[:4]:
        p, rows = 100.0, []
        for i in range(n):
            path = [p]
            for s in range(6):
                z = math.sqrt(rho) * common[i][s] + math.sqrt(1 - rho) * rng.gauss(0, 1)
                path.append(path[-1] * math.exp(z * vol / math.sqrt(6)))
            rows.append(Candle(time=T0 + i * H1_MS, open=path[0], high=max(path), low=min(path),
                               close=path[-1], volume=1.0))
            p = path[-1]
        out[sym] = rows
    return out


def test_the_trend_at_a_bar_never_reads_a_later_bar():
    c = _basket(1, n=6000)["BTCUSDT"]
    before = trend_by_bar(c)
    bent = c[:5000] + [Candle(time=x.time, open=1, high=1.1, low=0.9, close=1, volume=1) for x in c[5000:]]
    after = trend_by_bar(bent)
    assert before[:4990] == after[:4990]


def test_a_downtrend_shorts_only_and_every_trade_targets_at_least_1r():
    rows, p = [], 1000.0
    rng = random.Random(3)
    for i in range(6000):                       # steady decline with noise: a downtrend
        o = p
        p = p * math.exp(-0.0004 + rng.gauss(0, 0.006))
        rows.append(Candle(time=T0 + i * H1_MS, open=o, high=max(o, p) * 1.003, low=min(o, p) * 0.997,
                           close=p, volume=1.0))
    trades = run_rule(rows)
    assert trades and all(not t["long"] for t in trades)
    assert all(t["target_r"] >= 1.0 for t in trades)


def test_on_correlated_random_walks_the_rule_shows_no_edge():
    rep = backtest(series=_basket(5), cost_bps=20.0, draws=60)
    assert "does not beat" in rep["result"]["verdict"] or "INCONCLUSIVE" in rep["result"]["verdict"]
    assert "mechanical core" in render(rep)


def test_sweeptest_is_reachable_as_a_telegram_command(monkeypatch):
    def fake(symbols=None, client=None, cost_bps=20.0):
        return {"cost_bps": cost_bps, "error": "no 1h history could be fetched for any symbol", "sources": {}}

    monkeypatch.setattr(sweeptest, "backtest", fake)
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    assert CommandRouter(app).handle("/sweeptest").startswith("<pre>SWEEP-TEST")
    assert "/sweeptest" in CommandRouter(app).handle("/help")
