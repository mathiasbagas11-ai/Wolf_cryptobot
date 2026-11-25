"""Tests for /holdtest: v2's daily breakout against holding in BTC's regime.

They pin the arithmetic both portfolios rest on (a held coin's equity is its
price path less two costs; a breakout trade at 1% risk moves equity by about
1% of its R), that the regime is read at yesterday's close and never today's,
that nothing is ever shorted or levered, and that the bootstrap is paired.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from wolf import holdtest, trendtest
from wolf.bbtest import DEFAULT_SYMBOLS
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter

T0 = 1504224000000
DAY = 86_400_000


def _walk(seed, n=1400, drift=0.0, vol=0.035, rho=0.7, k=4, sub=16):
    rng = random.Random(seed)
    out = {}
    common = [[rng.gauss(0, 1) for _ in range(sub)] for _ in range(n)]
    for sym in DEFAULT_SYMBOLS[:k]:
        p, rows = 100.0, []
        for i in range(n):
            path = [p]
            st = vol / math.sqrt(sub)
            for s in range(sub):
                z = math.sqrt(rho) * common[i][s] + math.sqrt(1 - rho) * rng.gauss(0, 1)
                path.append(path[-1] * math.exp(drift / sub + z * st - st * st / 2))
            rows.append(Candle(time=T0 + i * DAY, open=path[0], high=max(path), low=min(path),
                               close=path[-1], volume=1.0))
            p = path[-1]
        out[sym] = rows
    return out


def _equity(rets):
    eq = 1.0
    for r in rets:
        eq *= 1 + r
    return eq


def test_holding_through_an_always_up_regime_is_the_coin_less_one_buy_cost():
    s = _walk(1, k=1, drift=0.002)
    times, by = holdtest._timeline(s)
    rets, expo = holdtest.hold_returns(s, {t: 1 for t in times}, times, by, 301)
    coin = s["BTCUSDT"][-1].open / s["BTCUSDT"][301].open
    assert abs(_equity(rets) - coin / (1 + holdtest.SIDE_COST)) < 1e-9
    assert min(expo) > 0.99


def test_the_hold_reads_yesterdays_regime_so_a_flip_today_moves_nothing_today():
    s = _walk(2, k=1)
    times, by = holdtest._timeline(s)
    only_today = {times[500]: 1}
    rets, expo = holdtest.hold_returns(s, only_today, times, by, 301)
    assert expo[500 - 301] == 0.0 and expo[501 - 301] > 0.99
    assert expo[502 - 301] == 0.0


def test_a_breakout_trade_moves_equity_by_one_percent_of_its_r_less_its_two_costs():
    s = _walk(7, k=1, drift=0.002, n=1600)
    up = {t: v for t, v in trendtest.btc_regime(s["BTCUSDT"]).items() if v > 0}
    times, by = holdtest._timeline(s)
    rets, expo, _ = holdtest.breakout_returns(s, up, times, by, 301)
    trades = [t for t in trendtest.run_trend(s["BTCUSDT"], up)
              if t["bar"] >= 301 and t["exit_bar"] < len(times) - 1]
    assert trades
    expected = 1.0
    for t in trades:
        w = min(holdtest.RISK / (t["risk_pct"] / 100), 1.0)
        expected *= 1 + holdtest.RISK * t["r"] - 2 * holdtest.SIDE_COST * w
    assert abs(_equity(rets) - expected) < 0.0005
    assert max(expo) <= 1.0 + 1e-9


def test_the_book_is_never_levered_even_when_every_coin_breaks_out_at_once():
    s = _walk(8, k=4, drift=0.003)
    up = {t: v for t, v in trendtest.btc_regime(s["BTCUSDT"]).items() if v > 0}
    times, by = holdtest._timeline(s)
    _, expo, _ = holdtest.breakout_returns(s, up, times, by, 301)
    assert max(expo) <= 1.0 + 1e-9


def test_the_bootstrap_is_paired_identical_series_never_separate():
    rng = random.Random(3)
    x = [rng.gauss(0.001, 0.02) for _ in range(800)]
    assert holdtest.bootstrap(x, x, draws=200) == 0.0


def test_on_correlated_random_walks_neither_side_is_declared_the_winner():
    r = holdtest.backtest(series=_walk(11, n=1800), draws=300)
    assert r["verdict"].startswith("no difference shown")


def test_the_card_names_both_portfolios_the_bar_and_btc_as_context():
    text = holdtest.render(holdtest.backtest(series=_walk(12, n=1200), draws=100))
    assert "HOLD-TEST" in text and "B breakout" in text and "H regime hold" in text
    assert "≥98.75%" in text and "BTC buy&hold" in text and "not tested" in text


def test_without_btc_the_card_says_there_is_no_regime():
    s = _walk(13)
    del s["BTCUSDT"]
    assert "no daily BTC history" in holdtest.render(holdtest.backtest(series=s))


def test_holdtest_is_reachable_as_a_telegram_command(monkeypatch):
    monkeypatch.setattr(holdtest, "backtest", lambda client=None: {"sources": {}, "error": "stub"})
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    router = CommandRouter(app)
    assert router.handle("/holdtest").startswith("<pre>HOLD-TEST")
    assert "/holdtest" in router.handle("/help")
