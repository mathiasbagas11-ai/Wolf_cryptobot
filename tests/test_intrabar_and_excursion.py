"""Tests for the two path measurements: bar-order dependence and MFE/MAE.

Both describe how trades travelled rather than where they ended. The intrabar
card asks how much of the ledger rests on guessing which side of a wide bar
traded first; the excursion fields record how far each position went for and
against it while open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.config import LadderSettings
from wolf.excursion import excursions, render as render_excursion
from wolf.models import Signal, Status
from wolf.notify.commands import CommandRouter
from wolf.tracker import OUTCOMES_KEY, PENDING_KEY, Tracker
from wolf.config import TrackerSettings
from wolf.whatif import compare_intrabar, render_intrabar

from tests.test_whatif import LADDER_1_3, _TP2_THEN_FADE, _router_app, _run

# A red bar that reaches TP1 (105) and the stop (95) of a 100/95 long. Its
# colour says the high came first, so the inferred rule banks TP1 and closes
# the rest at the breakeven stop; stop-first books the full loss.
_BOTH_SIDES = [(101, 106, 94, 100)]


def test_a_bar_through_both_sides_is_where_the_two_rules_part(store, fake_client):
    _run(store, fake_client, _BOTH_SIDES, LadderSettings())
    report = compare_intrabar(Tracker(store, fake_client, TrackerSettings()))
    inferred, stop_first = report["results"]
    assert inferred.mean_r == 0.5 and stop_first.mean_r == -1.0
    assert report["paired"]["changed"] == 1
    assert report["paired"]["mean_diff"] == -1.5


def test_a_path_that_never_straddles_a_bar_scores_the_same_both_ways(store, fake_client):
    """The tie count is what shows the instrument is not measuring itself."""
    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    report = compare_intrabar(Tracker(store, fake_client, TrackerSettings()))
    assert report["paired"]["changed"] == 0
    assert "tied 1" in render_intrabar(report)


def test_bars_after_the_timeout_are_not_graded(store, fake_client):
    """The live position was closed at its timeout; a stop printed hours
    later never happened to it."""
    from dataclasses import replace
    from wolf.models import Candle

    tracker = Tracker(store, fake_client, TrackerSettings())
    sig = tracker.record_signal("BTCUSDT", "MOMENTUM", "LONG", 100, tp=115, sl=95,
                                entry_mode="MOMENTUM_NOW", timeframe="15m",
                                tps=LADDER_1_3, entry_quoted_live=True)
    store.write(PENDING_KEY, [])
    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=sig.timeout_hours + 3)
    t0 = int(created.timestamp() // 900 * 900 * 1000)
    bars = int((now - created).total_seconds() // 900)
    cut = int(sig.timeout_hours * 4)
    fake_client.klines["BTCUSDT"] = [
        Candle(time=t0 + i * 900_000, open=100, high=101, low=99, close=100, volume=1.0)
        if i <= cut else
        Candle(time=t0 + i * 900_000, open=100, high=100, low=80, close=80, volume=1.0)
        for i in range(-1, bars)
    ]
    done = replace(sig, created_at=created.isoformat(), status=Status.SL_HIT.value,
                   r_multiple=-1.0, resolved_at=now.isoformat())
    store.write(OUTCOMES_KEY, [done.to_dict()])
    report = compare_intrabar(Tracker(store, fake_client, TrackerSettings()))
    assert report["open_at_cut"] == 1
    assert report["results"][0].mean_r == 0.0


def test_the_card_calls_stop_first_a_floor_not_the_truth(store, fake_client):
    _run(store, fake_client, _BOTH_SIDES, LadderSettings())
    card = render_intrabar(compare_intrabar(Tracker(store, fake_client, TrackerSettings())))
    assert "stop_first is the floor" in card


def test_a_resolved_trade_records_its_best_and_worst_point_in_r(store, fake_client):
    sig = _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    assert sig.mfe_r == 2.2      # high 111 against entry 100, stop distance 5
    assert sig.mae_r == 0.2      # low 99
    stored = Signal.from_dict(store.read(OUTCOMES_KEY)[-1])
    assert (stored.mfe_r, stored.mae_r) == (2.2, 0.2)


def test_an_open_trade_shows_its_path_so_far(store, fake_client):
    tracker = Tracker(store, fake_client, TrackerSettings())
    sig = tracker.record_signal("ETHUSDT", "MOMENTUM", "LONG", 100, tp=115, sl=95,
                                entry_mode="MOMENTUM_NOW", timeframe="15m")
    t0 = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    from wolf.models import Candle
    fake_client.klines["ETHUSDT"] = [
        Candle(time=t0 - 900_000, open=100, high=100, low=100, close=100, volume=1.0),
        Candle(time=t0 + 900_000, open=100, high=103, low=97, close=101, volume=1.0),
    ]
    assert tracker.check_pending() == []
    live = Signal.from_dict(store.read(PENDING_KEY)[0])
    assert (live.mfe_r, live.mae_r) == (0.6, 0.6)


def _row(r: float, mfe, mae, n: int) -> Signal:
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return Signal(id=f"x{n}", symbol="X", signal_type="SCREENER", direction="LONG",
                  entry_price=100, tp=110, sl=95, strategy="SCALP",
                  status=(Status.TP_HIT if r > 0 else Status.SL_HIT).value,
                  r_multiple=r, resolved_at=at, exit_time=at, mfe_r=mfe, mae_r=mae)


def test_rows_from_before_paths_were_recorded_are_left_out_by_name():
    rows = [_row(-1.0, 0.7, 1.0, 1), _row(-1.0, 0.1, 1.0, 2), _row(1.2, 1.5, 0.9, 3),
            _row(-1.0, None, None, 4)]
    report = excursions(rows)
    assert report["unrecorded"] == 1
    o = report["overall"]
    assert o["n"] == 3 and o["losers_were_green"] == 1 and o["winners_deep_dip"] == 1
    assert "left out: 1 rows" in render_excursion(report)


def test_an_empty_card_says_when_paths_start():
    assert "recorded from 2026-10-28" in render_excursion(excursions([]))


def test_both_commands_are_reachable_and_listed(store, fake_client):
    _run(store, fake_client, _BOTH_SIDES, LadderSettings())
    router = CommandRouter(_router_app(store, fake_client))
    assert "WHATIF intrabar" in router.handle("/whatif intrabar")
    assert "EXCURSION" in router.handle("/mae")
    help_text = router.handle("/help")
    assert "/mae" in help_text and "/whatif intrabar" in help_text


def test_the_card_names_the_deployment_that_wrote_it(monkeypatch, store, fake_client):
    """Two copies of the bot once posted look-alike cards to one chat for days."""
    from wolf.config import instance_label
    from wolf.diagnose import diagnose, render_digest

    monkeypatch.setenv("RAILWAY_PROJECT_NAME", "wolf")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "worker-2")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234")
    assert instance_label() == "railway wolf/worker-2/production @ abcdef1"
    _run(store, fake_client, _BOTH_SIDES, LadderSettings())
    card = render_digest(diagnose(Tracker(store, fake_client, TrackerSettings()),
                                  window_hours=24, state_dir="state_data"))
    assert "instance railway wolf/worker-2/production @ abcdef1" in card


def test_off_railway_the_label_says_so(monkeypatch):
    from wolf.config import instance_label

    for k in ("RAILWAY_PROJECT_NAME", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT_NAME"):
        monkeypatch.delenv(k, raising=False)
    assert instance_label().startswith("not on Railway")
