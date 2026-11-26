"""Tests for the OpenBot challenger referee (wolf.openbot).

The referee has to be the same for any caller: Wolf, not the agent, sets the
entry; a bar touching both levels is booked as the stop; a timeout closes at
the last bar inside the limit; every refusal is counted; and nothing reaches
the tracker.
"""

from __future__ import annotations

from types import SimpleNamespace

from wolf.models import Candle
from wolf.notify.commands import CommandRouter
from wolf.openbot import BAR_MS, STATE_KEY, OpenBotLedger, parse_command

T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR_MS)


def _bar(i, o, h, l, c):
    return Candle(time=T0 + i * BAR_MS, open=o, high=h, low=l, close=c, volume=1.0)


class _Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


def _ledger(store, data, clock):
    return OpenBotLedger(store, cost_bps=20.0, symbols=("BTCUSDT", "SOLUSDT"),
                         fetch=lambda s: data.get(s, []), now_ms=clock)


def _setup(store, path):
    """Bar 0 is forming at 100 when the call arrives; ``path`` follows."""
    data = {"SOLUSDT": [_bar(0, 100, 100, 100, 100)]}
    clock = _Clock(T0 + 60_000)
    ob = _ledger(store, data, clock)
    out = ob.submit("sol", "LONG", 98, 104, hours=2)
    assert out.get("recorded"), out
    data["SOLUSDT"] = [_bar(0, 100, 100.5, 99.5, 100)] + [
        _bar(i + 1, *ohlc) for i, ohlc in enumerate(path)]
    return ob, clock


def test_the_entry_is_wolfs_live_price_not_a_price_the_agent_names(store):
    data = {"SOLUSDT": [_bar(0, 99, 101, 99, 100.7)]}
    ob = _ledger(store, data, _Clock(T0 + 60_000))
    out = ob.submit("SOLUSDT", "long", 98, 104)
    assert out["entry"] == 100.7
    assert out["grade_from"] == T0 + BAR_MS


def test_a_target_hit_books_its_r_less_cost_on_the_stop(store):
    ob, clock = _setup(store, [(100, 101, 99.5, 100.8), (100.8, 104.5, 100.5, 104)])
    clock.ms = T0 + 3 * BAR_MS
    exits = ob.run()["exits"]
    assert len(exits) == 1 and exits[0]["how"] == "target"
    assert abs(exits[0]["r"] - 2.0) < 1e-9
    assert abs(exits[0]["net_r"] - (2.0 - 0.2 / 2.0)) < 1e-9


def test_a_bar_touching_both_levels_is_booked_as_the_stop(store):
    ob, clock = _setup(store, [(100, 105, 97, 101)])
    clock.ms = T0 + 2 * BAR_MS
    exits = ob.run()["exits"]
    assert exits[0]["how"] == "stop" and exits[0]["r"] == -1.0


def test_the_bar_the_call_arrived_in_is_never_graded(store):
    # Bar 0 (the entry bar) dips through the stop before the call; it must not count.
    data = {"SOLUSDT": [_bar(0, 100, 100, 100, 100)]}
    clock = _Clock(T0 + 60_000)
    ob = _ledger(store, data, clock)
    ob.submit("SOL", "LONG", 98, 104, hours=2)
    data["SOLUSDT"] = [_bar(0, 100, 100, 90, 100), _bar(1, 100, 101, 99, 100.5)]
    clock.ms = T0 + 2 * BAR_MS
    assert ob.run()["exits"] == []


def test_a_timeout_closes_at_the_last_bar_inside_the_limit(store):
    path = [(100, 101, 99, 100.5)] * 7 + [(100.5, 101, 100, 101)] + [(101, 110, 101, 110)]
    ob, clock = _setup(store, path)
    clock.ms = T0 + 20 * BAR_MS
    t = ob.run()["exits"][0]
    assert t["how"] == "timeout"
    # opened at T0+1min, 2h limit → bars 1..7 end by the deadline; bar 8 does not.
    assert abs(t["r"] - 0.25) < 1e-9


def test_an_open_position_waits_until_it_is_decided(store):
    ob, clock = _setup(store, [(100, 101, 99, 100.5)])
    clock.ms = T0 + 2 * BAR_MS
    assert ob.run()["exits"] == []
    assert "SOLUSDT" in ob.state()["open"]


def test_every_refusal_is_counted_by_reason(store):
    data = {"SOLUSDT": [_bar(0, 100, 100, 100, 100)], "BTCUSDT": [_bar(0, 100, 100, 100, 100)]}
    ob = _ledger(store, data, _Clock(T0 + 60_000))
    assert ob.submit("DOGE", "LONG", 1, 2)["refused"] == "coin_outside_universe"
    assert ob.submit("SOL", "LONG", 101, 104)["refused"] == "levels_wrong_side"
    assert ob.submit("SOL", "SHORT", 98, 104)["refused"] == "levels_wrong_side"
    assert ob.submit("SOL", "LONG", 99.95, 104)["refused"] == "stop_width"
    assert ob.submit("SOL", "LONG", 98, 104, hours=100)["refused"] == "bad_hours"
    assert ob.submit("SOL", "LONG", 98, 104).get("recorded")
    assert ob.submit("SOL", "LONG", 98, 104)["refused"] == "already_open"
    st = ob.state()
    assert st["received"] == 7 and sum(st["refused"].values()) == 6


def test_nothing_the_challenger_records_reaches_the_tracker(store, tracker):
    data = {"SOLUSDT": [_bar(0, 100, 100, 100, 100)]}
    ob = OpenBotLedger(store, symbols=("SOLUSDT",), fetch=lambda s: data[s],
                       now_ms=lambda: T0 + 60_000, tracker=tracker)
    ob.submit("SOL", "LONG", 98, 104)
    assert store.read(STATE_KEY, default=None)["open"]
    assert tracker.active_signals() == [] and tracker.outcomes() == []


def test_parse_command_reads_hours_and_reason():
    kw = parse_command("BTC SHORT 61,000 57000 12j breakdown di bawah support")
    assert kw == {"symbol": "BTC", "direction": "SHORT", "sl": "61000", "tp": "57000",
                  "hours": 12.0, "reason": "breakdown di bawah support"}
    assert parse_command("BTC LONG 1 2 alasan")["hours"] is None


def test_the_telegram_commands_record_and_report(store):
    data = {"SOLUSDT": [_bar(0, 100, 100, 100, 100)]}
    ob = OpenBotLedger(store, symbols=("SOLUSDT",), fetch=lambda s: data[s],
                       now_ms=lambda: T0 + 60_000)
    router = CommandRouter(SimpleNamespace(openbot=ob))
    assert router.handle("/ob SOL LONG 98 104 24 coba").startswith("✅")
    assert "Ditolak" in router.handle("/ob SOL LONG 98 104")
    card = router.handle("/openbot")
    assert "OPENBOT" in card and "SOLUSDT" in card and "100 trade" in card


def test_the_challenger_posts_nothing_to_telegram_unless_asked(settings):
    from wolf.app import build_application
    app = build_application(settings)
    assert app.openbot is not None and app.openbot._notifier is None
