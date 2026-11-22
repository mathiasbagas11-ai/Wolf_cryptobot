"""Tests for /zonetest: naive daily zones, their coverage, and the zone fade.

The point of the card is that coverage is not a win rate, so these pin down
that zones are causal (known at 00:00), that a fade is graded bar by bar with
the coarse 4h order charged against it, and that random-day zones calibrate.
"""

from __future__ import annotations

from types import SimpleNamespace

from wolf import zonetest
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter
from wolf.zonetest import DAY_MS, backtest, daily_bars, fade_trades, render, zones

H = 4 * 3600 * 1000


def _c(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c, volume=1.0)


def _day(d, ohlc_bars):
    return {"day": d, "bars": [_c(d * DAY_MS + i * H, *b) for i, b in enumerate(ohlc_bars)],
            "open": ohlc_bars[0][0], "close": ohlc_bars[-1][3]}


def test_only_complete_utc_days_are_kept():
    bars = [_c(i * H, 100, 101, 99, 100) for i in range(6 * 3 + 2)]
    assert len(daily_bars(bars)) == 3


def test_a_zone_never_reads_its_own_day():
    from tests.test_trendtest import _basket
    candles = _basket(1, n=6 * 120)["BTCUSDT"]
    days = daily_bars(candles)
    z1 = zones(days)
    days[100] = {**days[100], "high": days[100]["high"] * 3}
    z2 = zones(days)
    assert z1[100] == z2[100]          # day 100's zone ignores day 100
    assert z1[101] != z2[101] or z1[101] is None


def test_a_short_fade_that_returns_to_the_open_books_a_win():
    # open 100, adr 10, k_up 0.75: entry 105, stop 110, target 100 → +1R
    day = _day(5, [(100, 101, 99, 100), (100, 105.5, 100, 104), (104, 104, 99.5, 100)]
                  + [(100, 100, 100, 100)] * 3)
    t = fade_trades(day, 10.0, 0.75, 0.1)
    assert len(t) == 1 and t[0]["short"] and abs(t[0]["r"] - 1.0) < 1e-9


def test_a_bar_through_entry_and_stop_is_a_full_loss():
    day = _day(5, [(100, 111, 99.5, 108)] + [(108, 108, 108, 108)] * 5)
    t = fade_trades(day, 10.0, 0.75, 0.1)
    assert t[0]["short"] and abs(t[0]["r"] - (-1.0)) < 1e-9


def test_the_target_never_fills_on_the_entry_bar():
    day = _day(5, [(100, 105.5, 99, 103)] + [(103, 103, 103, 103)] * 5)
    t = fade_trades(day, 10.0, 0.75, 0.1)
    assert abs(t[0]["r"] - (105 - 103) / 5) < 1e-9     # held to the day's close


def test_on_random_walks_zones_on_their_own_day_beat_nothing():
    from tests.test_trendtest import _basket
    rep = backtest(series=_basket(4, n=6000), cost_bps=20.0, draws=60)
    assert rep["fade"]["n"] > 100 and "does not beat" in rep["fade"]["verdict"]
    assert "coverage is not a win rate" in render(rep)


def test_zonetest_is_reachable_as_a_telegram_command(monkeypatch):
    def fake(symbols=None, client=None, cost_bps=20.0):
        return {"cost_bps": cost_bps, "error": "no candle history could be fetched for any symbol",
                "sources": {}}

    monkeypatch.setattr(zonetest, "backtest", fake)
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None)
    assert CommandRouter(app).handle("/zonetest").startswith("<pre>ZONE-TEST")
    assert "/zonetest" in CommandRouter(app).handle("/help")
