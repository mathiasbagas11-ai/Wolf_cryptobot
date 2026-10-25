"""Tests for the Bollinger dip-rule backtest the owner brought from a video.

The rule is taken as stated, so most of these pin down that it is applied as
stated: the band window, the entry on the next open, the stop at the signal
candle's low, and the 20-bar exit. The baseline test is the one that matters
most — a long-only rule on a rising market looks good whether or not it is.
"""

from __future__ import annotations

from types import SimpleNamespace

from wolf import bbtest
from wolf.bbtest import (
    HOLD_BARS, PERIOD, _bands, _trade, backtest, is_refined_signal, random_baseline,
    render, run_rule,
)
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter

_H = 4 * 3600 * 1000


def _c(i, o, h, l, c):
    return Candle(time=i * _H, open=o, high=h, low=l, close=c, volume=1.0)


def _flat(n, price=100.0, wiggle=1.0):
    """A ranging market: closes alternate around ``price``."""
    out = []
    for i in range(n):
        c = price + (wiggle if i % 2 else -wiggle)
        out.append(_c(i, price, max(price, c) + 0.2, min(price, c) - 0.2, c))
    return out


def test_bands_use_only_closes_up_to_the_signal_bar():
    closes = [100.0] * PERIOD + [1000.0]
    mid, inner, outer = _bands(closes, PERIOD - 1)
    assert mid == 100.0 and inner == outer == 100.0   # the 1000 is not seen yet


def test_the_refined_rule_wants_a_long_green_candle_between_the_two_bands():
    bands = (100.0, 97.0, 96.0)          # mid, 1.5-band, 2.0-band
    good = _c(0, 97.5, 100.5, 96.5, 100.2)
    assert is_refined_signal(good, bands, atr=3.0)
    assert not is_refined_signal(_c(0, 100.2, 100.5, 96.5, 97.5), bands, 3.0)   # red
    assert not is_refined_signal(_c(0, 97.5, 100.5, 95.5, 100.2), bands, 3.0)   # through 2.0
    assert not is_refined_signal(_c(0, 97.5, 100.5, 97.5, 100.2), bands, 3.0)   # never below 1.5
    assert not is_refined_signal(good, bands, atr=10.0)                          # body not long
    assert not is_refined_signal(_c(0, 97.0, 100.5, 96.5, 97.6), bands, 3.0)    # body < half range


def test_a_trade_enters_on_the_next_open_and_stops_at_the_signal_low():
    candles = [_c(0, 100, 101, 95, 100)] + [_c(i, 100, 101, 99, 100) for i in range(1, 25)]
    candles[3] = _c(3, 99, 99, 94, 95)            # breaks the 95 low on bar 3
    r, risk_pct, exit_i = _trade(candles, 0)
    assert r == -1.0 and exit_i == 3 and risk_pct == 5.0


def test_a_gap_through_the_stop_is_booked_at_the_open_it_gapped_to():
    candles = [_c(0, 100, 101, 95, 100)] + [_c(i, 100, 101, 99, 100) for i in range(1, 25)]
    candles[2] = _c(2, 90, 91, 89, 90)
    r, _, _ = _trade(candles, 0)
    assert r == -2.0                               # (90 - 100) / 5


def test_a_trade_that_never_stops_exits_at_the_close_twenty_bars_on():
    candles = [_c(0, 100, 101, 95, 100)] + [_c(i, 100, 101, 99, 100) for i in range(1, 30)]
    candles[HOLD_BARS] = _c(HOLD_BARS, 100, 111, 99, 110)
    r, _, exit_i = _trade(candles, 0)
    assert exit_i == HOLD_BARS and r == 2.0        # (110 - 100) / 5


def test_only_one_position_is_held_at_a_time():
    candles = _flat(200)
    for i in range(40, 200, 3):                     # signals far closer than 20 bars
        candles[i] = _c(i, 99.2, 101.2, 98.6, 101.0)
    trades = run_rule(candles, "naive")
    times = [t["time"] for t in trades]
    assert all(b - a > HOLD_BARS * _H for a, b in zip(times, times[1:]))


def test_the_random_baseline_captures_drift_that_any_long_rule_would_get():
    """On a steadily rising market random longs win too — which is exactly
    why the rule is graded against them and not against zero."""
    rising = []
    for i in range(600):
        p = 100 * (1.004 ** i)
        rising.append(_c(i, p * 0.99, p * 1.01, p * 0.985, p * 1.008))
    means = random_baseline({"X": rising}, {"X": 30}, cost_bps=0, draws=50)
    assert min(means) > 0


def test_the_card_warns_when_it_only_had_months_not_years(monkeypatch):
    monkeypatch.setattr(bbtest, "fetch_history",
                        lambda sym, client=None, **_: (_flat(400), "fallback (last 1000 bars only)"))
    card = render(backtest(("BTCUSDT",)))
    assert "only ~5.5 months of history for BTCUSDT" in card
    assert "random" in card or "no trades" in card


def test_no_history_says_so_instead_of_printing_numbers(monkeypatch):
    monkeypatch.setattr(bbtest, "fetch_history", lambda sym, client=None, **_: ([], "unavailable"))
    assert "no candle history" in render(backtest(("BTCUSDT",)))


def test_the_command_runs_the_backtest_and_help_lists_it(monkeypatch):
    monkeypatch.setattr(bbtest, "fetch_history", lambda sym, client=None, **_: (_flat(400), "binance"))
    router = CommandRouter(SimpleNamespace(analyze=None, settings=Settings(), client=None))
    assert router.handle("/bbtest btc").startswith("<pre>BB-TEST")
    assert "/bbtest" in router.handle("/help")


def _walk(seed, n=8000, sub=16):
    """A driftless random walk whose bars are built from a continuous path,
    so a stop is filled where price actually crossed it. Bars made of single
    jumps let a stop fill at a price the walk never traded, which biased
    every R upward in proportion to how tight the stop was."""
    import random as _r
    rng = _r.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        o = hi = lo = p
        for _ in range(sub):
            p *= 1 + rng.gauss(0, 0.005)
            hi, lo = max(hi, p), min(lo, p)
        out.append(_c(i, o, hi, lo, p))
    return out


def test_on_a_random_walk_the_rule_lands_inside_its_matched_random_draws():
    """No pattern exists in a random walk, so an honest baseline has to put
    the rule somewhere inside its own distribution. The first baseline drew
    from every bar, tiny stops included, and put it above 96%."""
    share = backtest(series={s: _walk(50 + k) for k, s in enumerate("ABC")})["random"]["share_beaten"]
    assert 5 < share < 95
