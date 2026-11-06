"""Tests for the offline Elliott Wave wave-2 → wave-3 backtest.

A wave count drawn after the fact is always right, so most of these pin down
that the rule never acts on what it could not yet know: a pivot is usable from
the bar that confirmed it, the entry fills on the next open, and the baseline
shares every parameter but the entry moment.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

from wolf import ewtest
from wolf.config import Settings
from wolf.ewtest import EXT, backtest, find_setup, render, simulate, zigzag
from wolf.models import Candle
from wolf.notify.commands import CommandRouter

_H = 4 * 3600 * 1000


def _c(i, o, h, l, c):
    return Candle(time=i * _H, open=o, high=h, low=l, close=c, volume=1.0)


def _path(prices):
    """One bar per price step: open at the previous price, close at this one."""
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
        out.append(Candle(time=i * _H, open=path[0], high=max(path), low=min(path),
                          close=path[-1], volume=1.0))
        p = path[-1]
    return out


def test_a_pivot_is_usable_only_from_the_bar_that_confirmed_it():
    prices = [100.0] * 20 + [100 + i for i in range(1, 21)] + [120 - i for i in range(1, 15)]
    pivots = zigzag(_path(prices), k=3.0)
    high = next(p for p in pivots if p["kind"] == "H")
    assert high["px"] == 120.1
    assert high["confirmed"] > high["i"]       # known only after the reversal
    assert all(p["confirmed"] >= p["i"] for p in pivots)


def _wave1_then(prices_after):
    """Wave 1 from 100 to 120 at bar 40, then the given path."""
    prices = [100.0] * 20 + [100 + i for i in range(1, 21)] + prices_after + [115.0] * 70
    candles = _path(prices)
    p0 = {"kind": "L", "px": 100.0, "i": 20, "confirmed": 25}
    # The pivot is the bar's own high, not its close: 120.1.
    p1 = {"kind": "H", "px": 120.1, "i": 39, "confirmed": 42}
    return candles, p0, p1


def test_a_wave_2_in_the_zone_enters_on_the_next_open_after_a_close_above_the_prior_high():
    candles, p0, p1 = _wave1_then([117, 114, 111, 109, 110.5, 112])
    s = find_setup(candles, p0, p1)
    assert s is not None and s["long"]
    assert s["stop"] == 108.9                           # the wave-2 low
    assert s["entry"] == candles[s["bar"]].open
    assert s["bar"] == s["signal_bar"] + 1
    assert abs(s["target"] - (108.9 + EXT * 20.1)) < 1e-9


def test_a_retrace_deeper_than_61_8_percent_kills_the_setup():
    candles, p0, p1 = _wave1_then([116, 112, 108, 106, 109, 112])
    assert find_setup(candles, p0, p1) is None


def test_a_shallow_retrace_that_never_reaches_the_zone_never_triggers():
    candles, p0, p1 = _wave1_then([118, 116, 117, 119, 119.5])
    assert find_setup(candles, p0, p1) is None


def test_the_short_side_is_the_mirror_image():
    prices = [120.0] * 20 + [120 - i for i in range(1, 21)] + [103, 106, 109, 111, 109.5, 108] + [105.0] * 70
    candles = _path(prices)
    p0 = {"kind": "H", "px": 120.0, "i": 20, "confirmed": 25}
    p1 = {"kind": "L", "px": 99.9, "i": 39, "confirmed": 42}
    s = find_setup(candles, p0, p1)
    assert s is not None and not s["long"]
    assert s["stop"] == 111.1
    assert abs(s["target"] - (111.1 - EXT * 20.1)) < 1e-9


def test_a_bar_through_both_stop_and_target_is_booked_as_the_stop():
    candles = [_c(0, 100, 100, 100, 100), _c(1, 100, 130, 90, 100)] + \
              [_c(i, 100, 100, 100, 100) for i in range(2, 10)]
    r, exit_i, how = simulate(candles, 1, 100.0, 95.0, 110.0, long=True, hold=5)
    assert how == "stop" and r == -1.0 and exit_i == 1


def test_a_gap_through_the_stop_fills_at_the_open():
    candles = [_c(0, 100, 100, 100, 100), _c(1, 90, 91, 89, 90)] + \
              [_c(i, 90, 90, 90, 90) for i in range(2, 10)]
    r, _, how = simulate(candles, 1, 100.0, 95.0, 110.0, long=True, hold=5)
    assert how == "stop" and r == -2.0


def test_on_random_walks_the_rule_does_not_manufacture_an_edge():
    """No wave count can know anything on a random walk. Across seeds the
    rule must land in the middle of its own baseline, not above it."""
    beats = []
    for seed in range(3):
        series = {f"S{k}": _walk(seed * 10 + k) for k in range(3)}
        rep = backtest(series=series, cost_bps=20.0, draws=100)
        beats += [rep[side]["beats"] for side in ("long", "short") if "beats" in rep[side]]
    assert len(beats) == 6
    assert 15 <= sum(beats) / len(beats) <= 85


def test_the_card_names_its_baseline_and_its_bar():
    series = {"S0": _walk(1, n=3000)}
    card = render(backtest(series=series, cost_bps=20.0, draws=50))
    assert "random same coin/side/stop/target" in card
    assert "needs 97.5%" in card
    assert "changes no signal" in card


def test_ewtest_is_reachable_as_a_telegram_command(monkeypatch):
    seen = {}

    def fake(symbols, client=None, cost_bps=20.0):
        seen["symbols"] = symbols
        return {"cost_bps": cost_bps, "error": "no candle history could be fetched for any symbol",
                "sources": {}, "per_symbol": {}}

    monkeypatch.setattr(ewtest, "backtest", fake)
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    reply = CommandRouter(app).handle("/ewtest sol")
    assert reply.startswith("<pre>EW-TEST") and seen["symbols"] == ("SOLUSDT",)
    assert "/ewtest" in CommandRouter(app).handle("/help")
