"""Tests for v2, the trend-breakout rule run forward on paper.

The forward run must be the backtest, not a re-implementation of it: these
check it enters only on a closed breakout with BTC's regime, fills at the
next bar's open, closes on exactly the R the backtest would book, never
re-reads a bar it has read, and honours its pre-registered kill rule.
"""

from __future__ import annotations

from types import SimpleNamespace

from wolf import v2 as v2mod
from wolf.config import Settings
from wolf.models import Candle
from wolf.notify.commands import CommandRouter
from wolf.trendtest import HOLD_BARS, simulate_trail
from wolf.v2 import BAR_MS, LABEL, STATE_KEY, V2Engine

T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR_MS)


def _bar(i, o, h, l, c):
    return Candle(time=T0 + i * BAR_MS, open=o, high=h, low=l, close=c, volume=1.0)


def _btc(n, up=True):
    out = []
    for i in range(n):
        p = 100 + (0.05 * i if up else -0.05 * i)
        out.append(_bar(i, p, p + 0.3, p - 0.3, p + (0.02 if up else -0.02)))
    return out


def _flat_then_break(n, up=True):
    """Flat at 50 with a 1-point range, then the last closed bar breaks out."""
    bars = [_bar(i, 50, 50.5, 49.5, 50) for i in range(n - 2)]
    if up:
        bars.append(_bar(n - 2, 50, 53, 50, 52.5))     # closes above every prior high
        bars.append(_bar(n - 1, 52.6, 52.8, 52.4, 52.7))  # forming bar: the fill
    else:
        bars.append(_bar(n - 2, 50, 50, 47, 47.5))
        bars.append(_bar(n - 1, 47.4, 47.6, 47.2, 47.3))
    return bars


class _Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, text, thread_id=""):
        self.sent.append((text, thread_id))
        return True


def _engine(store, data, now_bar, notifier=None):
    return V2Engine(store, notifier=notifier, cost_bps=20.0, thread_id="77",
                    symbols=tuple(data), fetch=lambda s: data[s],
                    now_ms=lambda: T0 + now_bar * BAR_MS + 3600_000)


def test_a_breakout_with_btc_rising_opens_a_long_filled_at_the_next_open(store):
    n = 400
    data = {"BTCUSDT": _btc(n), "SOLUSDT": _flat_then_break(n)}
    note = _Notifier()
    out = _engine(store, data, n - 1, note).run()
    sol = [e for e in out["entries"] if e["symbol"] == "SOLUSDT"]
    assert sol and sol[0]["long"] and sol[0]["entry"] == 52.6
    assert sol[0]["entry_time"] == data["SOLUSDT"][-1].time
    assert any(LABEL in t and "SOLUSDT" in t and th == "77" for t, th in note.sent)


def test_a_short_breakdown_is_refused_while_btc_is_rising(store):
    n = 400
    data = {"BTCUSDT": _btc(n), "SOLUSDT": _flat_then_break(n, up=False)}
    out = _engine(store, data, n - 1).run()
    assert not [e for e in out["entries"] if e["symbol"] == "SOLUSDT"]


def test_the_same_closed_bar_is_never_read_twice(store):
    n = 400
    data = {"BTCUSDT": _btc(n), "SOLUSDT": _flat_then_break(n)}
    eng = _engine(store, data, n - 1)
    eng.run()
    st = store.read(STATE_KEY)
    st["open"] = {}
    store.write(STATE_KEY, st)
    assert not eng.run()["entries"]


def test_a_closed_trade_books_exactly_the_r_the_backtest_would(store):
    n = 400
    sol = _flat_then_break(n)
    data = {"BTCUSDT": _btc(n + 40), "SOLUSDT": sol}
    eng = _engine(store, {"BTCUSDT": data["BTCUSDT"][:n], "SOLUSDT": sol}, n - 1)
    pos = eng.run()["entries"][0]
    # the market then rallies, then collapses through the trail
    later = [_bar(n - 1, 52.6, 52.8, 52.4, 52.7)]
    p = 52.7
    for j in range(n, n + 10):
        later.append(_bar(j, p, p + 1.2, p - 0.1, p + 1.0))
        p += 1.0
    for j in range(n + 10, n + 30):
        later.append(_bar(j, p, p + 0.1, p - 3.0, p - 2.8))
        p -= 2.8
    full = sol[:-1] + later
    eng2 = _engine(store, {"BTCUSDT": data["BTCUSDT"][:len(full)], "SOLUSDT": full}, len(full) - 1)
    closed = eng2.run()["exits"]
    assert closed and closed[0]["symbol"] == "SOLUSDT"
    k = n - 1
    expected = simulate_trail(full, k, pos["risk"], True, hold=HOLD_BARS) \
        if k + HOLD_BARS <= len(full) else None
    from wolf.trendtest import walk_trail
    hit, _ = walk_trail(full, k, pos["risk"], True, len(full) - 1)
    assert abs(closed[0]["r"] - round(hit[0], 4)) < 1e-9
    assert expected is None or abs(expected[0] - hit[0]) < 1e-9


def test_a_trade_still_open_after_fifteen_days_closes_at_the_close(store):
    n = 400
    sol = _flat_then_break(n)
    eng = _engine(store, {"BTCUSDT": _btc(n), "SOLUSDT": sol}, n - 1)
    eng.run()
    calm = sol[:-1] + [_bar(n - 1 + j, 52.6, 52.9, 52.5, 52.7) for j in range(HOLD_BARS + 2)]
    eng2 = _engine(store, {"BTCUSDT": _btc(len(calm)), "SOLUSDT": calm}, len(calm) - 1)
    exits = eng2.run()["exits"]
    assert exits and exits[0]["how"] == "timeout"


def test_the_kill_rule_stops_new_entries_after_300_losing_trades(store):
    note = _Notifier()
    eng = _engine(store, {"BTCUSDT": _btc(400), "SOLUSDT": _flat_then_break(400)}, 399, note)
    st = eng.state()
    st["closed"] = [{"net_r": -0.1, "long": True}] * v2mod.KILL_AFTER
    store.write(STATE_KEY, st)
    out = eng.run()
    assert store.read(STATE_KEY)["killed"]["trades"] == v2mod.KILL_AFTER
    assert any("Aturan stop kena" in t for t, _ in note.sent)
    assert not eng.run()["entries"]


def test_v2_owns_the_high_conviction_room_when_on(monkeypatch):
    from wolf.app import build_application
    monkeypatch.setenv("CONVICTION_RANKING_ENABLED", "true")
    app = build_application(Settings.from_env())
    assert app.v2 is not None and app.conviction is None
    assert app.notifier.divert_high_conviction is False


def test_v2_is_reachable_as_a_telegram_command(store):
    eng = V2Engine(store, symbols=("BTCUSDT",), fetch=lambda s: [])
    app = SimpleNamespace(settings=Settings(), client=None, analyze=None, v2=eng)
    reply = CommandRouter(app).handle("/v2")
    assert reply.startswith("<pre>") and "V2 TREND" in reply and "300" in reply
    assert "/v2" in CommandRouter(app).handle("/help")


def test_the_dashboard_endpoint_serves_the_v2_ledger(settings):
    from fastapi.testclient import TestClient
    from wolf.api import create_app
    from wolf.app import build_application
    app_obj = build_application(settings)
    st = app_obj.v2.state()
    st["closed"] = [{"symbol": "SOLUSDT", "long": True, "entry": 100.0, "r": 2.0, "net_r": 1.95,
                     "how": "stop", "opened": "2026-11-22T00:00:00+00:00",
                     "exit_time": "2026-11-23T00:00:00+00:00"}]
    app_obj.store.write(STATE_KEY, st)
    body = TestClient(create_app(app_obj)).get("/v2").json()
    assert body["enabled"] and body["start_balance"] == 10_000.0 and body["n"] == 1
    assert body["curve"][0]["net"] == 1.95 and body["long"]["n"] == 1


def test_a_trending_series_is_labelled_persist_and_a_choppy_one_not():
    from wolf.v2 import persistence
    import math as _m
    trend, p, r = [], 100.0, 0.0
    import random as _r
    g = _r.Random(1)
    for _ in range(400):
        r = 0.6 * r + g.gauss(0, 0.01)       # positively autocorrelated returns
        p *= _m.exp(r)
        trend.append(p)
    chop, p, r = [], 100.0, 0.0
    for _ in range(400):
        r = -0.6 * r + g.gauss(0, 0.01)      # negatively autocorrelated returns
        p *= _m.exp(r)
        chop.append(p)
    assert persistence(trend)["label"] == "PERSIST"
    assert persistence(chop)["label"] == "NOT"
    assert persistence(trend[:50]) is None


def test_every_v2_entry_carries_the_persistence_label_and_it_is_never_a_filter(store):
    n = 400
    data = {"BTCUSDT": _btc(n), "SOLUSDT": _flat_then_break(n)}
    out = _engine(store, data, n - 1, _Notifier()).run()
    sol = [e for e in out["entries"] if e["symbol"] == "SOLUSDT"][0]
    assert "persist" in sol            # recorded (None when the history is flat)
    assert "Label tren" in _engine(store, data, n - 1)._entry_text("SOLUSDT", sol)


# ── the pulse: telling a quiet market from a stopped job ──────────────────

def test_a_pass_records_when_it_ran_and_how_far_each_coin_is_from_its_breakout(store):
    data = {"BTCUSDT": _btc(320), "ETHUSDT": [_bar(i, 50, 50.5, 49.5, 50) for i in range(320)]}
    eng = _engine(store, data, now_bar=319)
    eng.run()
    lp = store.read(STATE_KEY)["last_pass"]
    assert lp["fetched"] == 2 and lp["of"] == 2 and lp["missing"] == []
    assert lp["btc"]["side"] == "LONG" and lp["btc"]["sma"] is not None
    eth = next(c for c in lp["coins"] if c["symbol"] == "ETHUSDT")
    # flat at 50 with highs at 50.5: the close still needs +1% to break out long
    assert eth["level"] == 50.5 and abs(eth["gap_pct"] - 1.0) < 1e-9
    assert eng.summary()["last_pass"] == lp


def test_a_coin_that_broke_out_reads_a_gap_at_or_below_zero_and_is_marked_held(store):
    data = {"BTCUSDT": _btc(320), "SOLUSDT": _flat_then_break(320)}
    eng = _engine(store, data, now_bar=319)
    eng.run()
    sol = next(c for c in store.read(STATE_KEY)["last_pass"]["coins"] if c["symbol"] == "SOLUSDT")
    assert sol["gap_pct"] <= 0 and sol["open"] is True


def test_a_coin_whose_bars_failed_is_named_on_the_card_not_silently_dropped(store):
    data = {"BTCUSDT": _btc(320), "ETHUSDT": []}
    eng = _engine(store, data, now_bar=319)
    eng.run()
    card = eng.card()
    assert "data 1/2 koin" in card and "GAGAL: ETHUSDT" in card
    assert "jam lalu" in card


def test_a_pass_without_btc_is_still_recorded_as_skipped(store):
    eng = _engine(store, {"BTCUSDT": [], "ETHUSDT": _btc(320)}, now_bar=319)
    assert eng.run() == {"skipped": "no BTC bars"}
    assert "pass dilewati: no BTC bars" in eng.card()


def test_a_card_before_any_pass_says_the_job_has_not_run(store):
    eng = _engine(store, {"BTCUSDT": _btc(320)}, now_bar=319)
    assert "belum ada pass tercatat" in eng.card()
