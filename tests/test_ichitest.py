"""Tests for the offline Ichimoku backtest (/ichitest).

Most of these pin down causality: the cloud showing at a bar was computed 26
bars earlier, a cross is acted on at the next open, and the baseline shares
every parameter with the rule except the bar it enters on.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from wolf import ichitest
from wolf.config import Settings
from wolf.ichitest import (BEAT_LINE, SHIFT, backtest, ichimoku, render, run_rule,
                           signals)
from wolf.models import Candle
from wolf.notify.commands import CommandRouter

_H = 4 * 3600 * 1000
_T0 = 1577836800000          # 2020-01-01


def _c(i, o, h, l, c):
    return Candle(time=_T0 + i * _H, open=o, high=h, low=l, close=c, volume=1.0)


def _path(prices):
    out, prev = [], prices[0]
    for i, p in enumerate(prices):
        out.append(_c(i, prev, max(prev, p) + 0.1, min(prev, p) - 0.1, p))
        prev = p
    return out


def _walk(seed, n=6000, vol=0.012):
    rng = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        path = [p]
        for _ in range(8):
            path.append(path[-1] * math.exp(rng.gauss(0, vol / math.sqrt(8))))
        out.append(Candle(time=_T0 + i * _H, open=path[0], high=max(path), low=min(path),
                          close=path[-1], volume=1.0))
        p = path[-1]
    return out


def test_the_cloud_at_a_bar_is_the_one_computed_26_bars_earlier():
    candles = _walk(3, n=300)
    lines = ichimoku(candles)
    i = 200
    src = i - SHIFT
    hi9 = max(c.high for c in candles[src - 8:src + 1]); lo9 = min(c.low for c in candles[src - 8:src + 1])
    hi26 = max(c.high for c in candles[src - 25:src + 1]); lo26 = min(c.low for c in candles[src - 25:src + 1])
    hi52 = max(c.high for c in candles[src - 51:src + 1]); lo52 = min(c.low for c in candles[src - 51:src + 1])
    a = ((hi9 + lo9) / 2 + (hi26 + lo26) / 2) / 2
    b = (hi52 + lo52) / 2
    assert abs(lines[i]["top"] - max(a, b)) < 1e-9
    assert abs(lines[i]["bottom"] - min(a, b)) < 1e-9


def test_changing_a_later_bar_never_changes_an_earlier_signal():
    candles = _walk(5, n=600)
    before = [s for s in signals(candles, "tk_cross") if s[0] < 400]
    bent = candles[:450] + [_c(i, 1.0, 1.1, 0.9, 1.0) for i in range(450, 600)]
    assert [s for s in signals(bent, "tk_cross") if s[0] < 400] == before


def test_a_rally_out_of_a_long_base_gives_a_long_cloud_breakout_filled_next_open():
    prices = [100.0] * 120 + \
             [100 + 0.8 * i for i in range(1, 40)] + [131.0] * 80
    candles = _path(prices)
    sig = signals(candles, "cloud_break")
    first = next(s for s in sig if s[0] >= 120)
    assert first[1] is True
    trades = run_rule(candles, "cloud_break")
    t = next(t for t in trades if t["bar"] > 120)
    assert t["long"] and t["bar"] == first[0] + 1


def test_the_short_side_is_the_mirror_image():
    prices = [100.0] * 120 + \
             [100 - 0.8 * i for i in range(1, 40)] + [69.0] * 80
    sig = [s for s in signals(_path(prices), "tk_cross") if s[0] >= 120]
    assert sig and sig[0][1] is False


def test_on_random_walks_neither_rule_manufactures_an_edge():
    beats = []
    for seed in range(3):
        rep = backtest(series={f"S{k}": _walk(seed * 10 + k) for k in range(3)},
                       cost_bps=20.0, draws=100)
        for out in rep["rules"].values():
            beats += [out[s]["beats"] for s in ("long", "short") if "beats" in out[s]]
    assert len(beats) == 12
    assert 15 <= sum(beats) / len(beats) <= 85


def test_a_pass_needs_both_halves_of_the_out_of_sample_cut_positive():
    s = {"n": 500, "net": 0.2, "beats": 99.9,
         "before": {"n": 300, "net": 0.4}, "after": {"n": 200, "net": -0.06}}
    assert "does not beat" in ichitest._verdict(s)
    s["after"]["net"] = 0.05
    assert "PASSES" in ichitest._verdict(s)


def test_the_card_names_its_baseline_its_bar_and_its_cut():
    card = render(backtest(series={"S0": _walk(1, n=3000)}, cost_bps=20.0, draws=50))
    assert "random same coin/side/stop/target/hour" in card
    assert f"needs {BEAT_LINE:g}%" in card
    assert "out-of-sample cut: 2024" in card
    assert "changes no signal" in card


def test_ichitest_is_reachable_as_a_telegram_command(monkeypatch):
    seen = {}

    def fake(symbols, client=None, cost_bps=20.0):
        seen["symbols"] = symbols
        return {"cost_bps": cost_bps, "error": "no candle history could be fetched for any symbol",
                "sources": {}, "rules": {}}

    monkeypatch.setattr(ichitest, "backtest", fake)
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    reply = CommandRouter(app).handle("/ichitest eth")
    assert reply.startswith("<pre>ICHI-TEST") and seen["symbols"] == ("ETHUSDT",)
    assert "/ichitest" in CommandRouter(app).handle("/help")
