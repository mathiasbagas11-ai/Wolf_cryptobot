"""Tests for /trendtest, the offline read of the two new-strategy candidates.

They pin down what a backtest most easily gets wrong: the regime and the
breakout use only bars that had closed, the trailing stop never loosens and
is tested before it moves, the momentum book longs the leaders and shorts the
laggards, and neither baseline manufactures an edge on random walks.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from wolf import trendtest
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter
from wolf.trendtest import (BEAT_LINE, REGIME_BARS, backtest, btc_regime, momentum_weeks,
                            render, simulate_trail)

_H = 4 * 3600 * 1000
_T0 = 1604620800000          # 2020-11-06 00:00 UTC


def _c(i, o, h, l, c):
    return Candle(time=_T0 + i * _H, open=o, high=h, low=l, close=c, volume=1.0)


def _basket(seed, n=4000, rho=0.7, vol=0.02, drifts=None):
    rng = random.Random(seed)
    common = [[rng.gauss(0, 1) for _ in range(8)] for _ in range(n)]
    out = {}
    for k, sym in enumerate(trendtest.DEFAULT_SYMBOLS):
        p, rows, mu = 100.0, [], (drifts or {}).get(sym, 0.0)
        for i in range(n):
            path = [p]
            for s in range(8):
                z = math.sqrt(rho) * common[i][s] + math.sqrt(1 - rho) * rng.gauss(0, 1)
                path.append(path[-1] * math.exp(mu / 8 + z * vol / math.sqrt(8)))
            rows.append(Candle(time=_T0 + i * _H, open=path[0], high=max(path), low=min(path),
                               close=path[-1], volume=1.0))
            p = path[-1]
        out[sym] = rows
    return out


def test_the_regime_at_a_bar_never_reads_a_later_bar():
    btc = _basket(1, n=800)["BTCUSDT"]
    before = btc_regime(btc)
    bent = btc[:600] + [_c(i, 1.0, 1.1, 0.9, 1.0) for i in range(600, 800)]
    after = btc_regime(bent)
    assert all(after[c.time] == before[c.time] for c in btc[REGIME_BARS:600])


def test_the_trailing_stop_follows_the_best_price_and_never_loosens():
    up = [_c(i, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i + 0.4) for i in range(12)]
    down = [_c(12 + j, 111 - 2 * j, 111 - 2 * j + 0.2, 111 - 2 * j - 2, 110 - 2 * j) for j in range(20)]
    r, exit_i, how = simulate_trail(up + down, 1, risk=2.0, long=True, hold=30)
    assert how == "stop"
    # best high 111.5, trail 3.0 → stop 108.5, well above the 99 it started at
    assert r == (108.5 - 101.0) / 2.0


def test_a_bar_is_tested_against_the_stop_set_before_it():
    """A bar cannot raise its own stop: a big green bar whose low touches the
    old stop is stopped at the old stop, not saved by its own high."""
    bars = [_c(0, 100, 100, 100, 100), _c(1, 100, 130, 97.9, 129)] + \
           [_c(i, 129, 129, 129, 129) for i in range(2, 10)]
    r, exit_i, how = simulate_trail(bars, 1, risk=2.0, long=True, hold=5)
    assert how == "stop" and exit_i == 1 and r == -1.0


def test_the_momentum_book_longs_the_leaders_and_shorts_the_laggards():
    drifts = {s: (0.004 if k < 3 else -0.004 if k >= 7 else 0.0)
              for k, s in enumerate(trendtest.DEFAULT_SYMBOLS)}
    weeks = momentum_weeks(_basket(3, n=1200, drifts=drifts, vol=0.005), cost_bps=0.0)
    assert weeks
    leaders = set(trendtest.DEFAULT_SYMBOLS[:3])
    laggards = set(trendtest.DEFAULT_SYMBOLS[7:])
    hits = sum(set(w["ranked"][-3:]) == leaders and set(w["ranked"][:3]) == laggards for w in weeks)
    assert hits / len(weeks) > 0.8
    assert sum(w["net"] for w in weeks) > 0


def test_on_correlated_random_walks_neither_candidate_manufactures_an_edge():
    beats_a, beats_b = [], []
    for seed in range(3):
        rep = backtest(series=_basket(seed), cost_bps=20.0, draws=60)
        beats_a.append(rep["trend"]["beats"])
        beats_b.append(rep["momentum"]["beats"])
    assert 10 <= sum(beats_a) / 3 <= 90
    assert 10 <= sum(beats_b) / 3 <= 90


def test_the_card_names_both_baselines_and_the_bar():
    card = render(backtest(series=_basket(5, n=3000), cost_bps=20.0, draws=30))
    assert "shifted by a random number of days" in card
    assert "same weeks, random 3 long / 3 short" in card
    assert f"needs {BEAT_LINE:g}%" in card and "funding not modelled" in card
    assert "changes no signal" in card


def test_trendtest_is_reachable_as_a_telegram_command(monkeypatch):
    def fake(symbols=None, client=None, cost_bps=20.0):
        return {"cost_bps": cost_bps, "error": "no candle history could be fetched for any symbol",
                "sources": {}}

    monkeypatch.setattr(trendtest, "backtest", fake)
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    assert CommandRouter(app).handle("/trendtest").startswith("<pre>TREND-TEST")
    assert "/trendtest" in CommandRouter(app).handle("/help")
