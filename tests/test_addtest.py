"""Tests for /addtest: averaging down with a half-gap exit, paired against plain.

The added lot must be the atlas's lot (closed on its own at the half-gap
target), it must never change the first unit's trade, and the instrument
must not credit averaging with an edge on random walks.
"""

from __future__ import annotations

from types import SimpleNamespace

from wolf import addtest
from wolf.addtest import backtest, render, simulate_add_lot
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter

H = 4 * 3600 * 1000


def _c(i, o, h, l, c):
    return Candle(time=i * H, open=o, high=h, low=l, close=c, volume=1.0)


def _flat(i, p):
    return _c(i, p, p, p, p)


def test_the_added_lot_banks_the_half_gap_on_a_bounce():
    # entry 100, risk 10 → add at 95, target 97.5
    bars = [_flat(0, 100), _c(1, 100, 100, 94, 96), _c(2, 96, 98, 96, 97)] + [_flat(i, 97) for i in range(3, 10)]
    lot = simulate_add_lot(bars, 0, 10.0, True, hold=8)
    assert lot["added"] and lot["how"] == "target" and abs(lot["r"] - 0.25) < 1e-9


def test_the_target_never_fills_on_the_bar_that_added():
    # the add bar's own high (99) crosses the 97.5 target, later bars never do
    bars = [_flat(0, 100), _c(1, 100, 99, 94, 96)] + [_flat(i, 96) for i in range(2, 10)]
    lot = simulate_add_lot(bars, 0, 10.0, True, hold=8)
    assert lot["added"] and lot["how"] == "timeout"


def test_a_slide_through_the_stop_loses_the_lot_half_a_risk_unit():
    bars = [_flat(0, 100), _c(1, 100, 100, 94, 95), _c(2, 95, 95, 89, 89)] + [_flat(i, 89) for i in range(3, 10)]
    lot = simulate_add_lot(bars, 0, 10.0, True, hold=8)
    assert lot["how"] == "stop" and abs(lot["r"] - (-0.5)) < 1e-9


def test_no_add_fires_when_price_never_comes_back_against_the_entry():
    bars = [_flat(0, 100)] + [_c(i, 100 + i, 101 + i, 100 + i, 101 + i) for i in range(1, 12)]
    assert simulate_add_lot(bars, 0, 10.0, True, hold=10) == {"added": False}


def test_the_short_side_is_the_mirror_image():
    bars = [_flat(0, 100), _c(1, 100, 106, 100, 104), _c(2, 104, 104, 102, 103)] + [_flat(i, 103) for i in range(3, 10)]
    lot = simulate_add_lot(bars, 0, 10.0, False, hold=8)
    assert lot["added"] and lot["how"] == "target" and abs(lot["r"] - 0.25) < 1e-9


def test_on_random_walks_averaging_down_shows_no_edge():
    from tests.test_trendtest import _basket
    rep = backtest(series=_basket(2, n=3000), cost_bps=20.0)
    assert rep["n"] > 100
    assert not (rep["gap"] > 0 and rep["t"] >= 2)
    assert "does not improve" in render(rep)


def test_addtest_is_reachable_as_a_telegram_command(monkeypatch):
    def fake(symbols=None, client=None, cost_bps=20.0):
        return {"cost_bps": cost_bps, "error": "no candle history could be fetched for any symbol",
                "sources": {}}

    monkeypatch.setattr(addtest, "backtest", fake)
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    assert CommandRouter(app).handle("/addtest").startswith("<pre>ADD-TEST")
    assert "/addtest" in CommandRouter(app).handle("/help")
