"""A displaced candidate is entered at the same price as the winner.

The winner is re-quoted at the live price before it is recorded; the losers
used to be snapshotted one step earlier, at the bar close. Every contest then
compared a winner entered late — on a stretched risk unit, with its ladder
pushed out — against a loser filled at a price that no longer existed, and the
loser won any outcome that was not a mutual stop-out. TRAP and SCALP share
their entry, stop and ladder rule exactly and still split 16 ahead to 34
behind on the first read. These run the screener's real re-quote path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from wolf.config import TrackerSettings
from wolf.contest_audit import (
    CONTESTS_KEY, _is_gradeable, _loser_signal, audit_contests, render,
)
from wolf.detectors.base import SignalCandidate
from wolf.models import Signal, Status
from wolf.screener import Screener, _loser_spec
from wolf.tracker import OUTCOMES_KEY, Tracker

_LADDER = [
    {"level": 1, "price": 105.0, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 110.0, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 115.0, "allocation": 0.2, "r_multiple": 3.0},
]


def _cand(strategy, score, *, max_chase_r=None, entry_mode="MOMENTUM_NOW"):
    c = SignalCandidate(
        symbol="BTCUSDT", signal_type=strategy, direction="LONG",
        entry_price=100.0, tp=115.0, sl=95.0, score=score, strategy=strategy,
        reasons=["x"], entry_mode=entry_mode, timeframe="15m",
        tps=[dict(r) for r in _LADDER],
    )
    if max_chase_r is not None:
        c.max_chase_r = max_chase_r
    return c


def _contest_as_screened(fake_client, store, live, loser):
    """Exactly what _best_candidate then _reprice_at_market do to a pair."""
    winner = _cand("TRAP", 90)
    winner.losers = [_loser_spec(loser)]
    fake_client.prices["BTCUSDT"] = live
    screener = Screener(fake_client, Tracker(store, fake_client, TrackerSettings()), [])
    assert screener._reprice_at_market(winner) is False
    return winner


def test_two_candidates_with_one_geometry_become_one_trade(store, fake_client):
    """TRAP and SCALP place entry, stop and ladder by the same rule. Moved by
    the same re-quote they are the same trade, so the contest can only tie —
    which is what an honest instrument should report for them."""
    winner = _contest_as_screened(fake_client, store, 101.0, _cand("SCALP", 70))
    spec = winner.losers[0]

    assert spec["requote"] == "live"
    assert spec["quoted_entry"] == 100.0
    loser = _loser_signal({"symbol": "BTCUSDT",
                           "at": datetime.now(timezone.utc).isoformat()}, spec)
    assert loser.entry_price == winner.entry_price == 101.0
    assert loser.sl == winner.sl
    assert [r["price"] for r in loser.tp_ladder] == [r["price"] for r in winner.tps]


def test_a_loser_that_could_not_have_been_entered_is_not_graded(store, fake_client):
    """Price ran 0.8R. The winner's own limit allowed it here; a loser on the
    default 0.5R would have been dropped by the same rule, so grading it on a
    fill would invent a trade the bot could never have taken."""
    winner = _cand("PREPUMP", 90, max_chase_r=1.5)
    winner.losers = [_loser_spec(_cand("SCALP", 40))]
    fake_client.prices["BTCUSDT"] = 104.0
    Screener(fake_client, Tracker(store, fake_client, TrackerSettings()), [])._reprice_at_market(winner)

    spec = winner.losers[0]
    assert spec["requote"] == "chase_drop"
    assert spec["entry_price"] == 100.0      # untouched: it was never entered
    row = {"symbol": "BTCUSDT", "at": "2026-10-01T00:00:00+00:00"}
    assert not _is_gradeable(row, spec, TrackerSettings(), datetime.now(timezone.utc))


def test_a_pending_level_is_left_at_its_level(store, fake_client):
    """A limit order at a level is a fillable trade without any re-quote."""
    winner = _contest_as_screened(fake_client, store, 101.0,
                                  _cand("SWING", 60, entry_mode="RETEST_WAIT"))
    assert winner.losers[0]["requote"] == "level"
    assert winner.losers[0]["entry_price"] == 100.0


def _winner_outcome(store, *, quoted_live):
    sig = Signal(symbol="BTCUSDT", signal_type="TRAP", direction="LONG",
                 entry_price=101.0, tp=119.0, sl=95.0, strategy="TRAP",
                 status=Status.SL_HIT.value, exit_price=95.0, pnl_pct=-5.94,
                 entry_quoted_live=quoted_live)
    sig.id = "w1"
    store.write(OUTCOMES_KEY, [sig.to_dict()])


def _row(spec):
    return {"at": "2026-10-01T00:00:00+00:00", "symbol": "BTCUSDT", "signal_id": "w1",
            "winner": {"strategy": "TRAP", "score": 90, "direction": "LONG"},
            "losers": [spec]}


def test_pairs_recorded_before_the_fix_are_left_out_by_name(store, fake_client):
    """Their loser was priced at the bar close and their winner at the live
    re-quote. Read alongside fixed rows they would carry the bias back in."""
    _winner_outcome(store, quoted_live=True)
    legacy = {**_loser_spec(_cand("SCALP", 70)), "graded": {"r": 0.5, "resolved": True}}
    store.write(CONTESTS_KEY, [_row(legacy)])

    report = audit_contests(Tracker(store, fake_client, TrackerSettings()))
    assert report["skipped"]["legacy"] == 1 and report["pairs"] == []


def test_a_contest_whose_winner_was_never_requoted_is_still_read(store, fake_client):
    """Both sides at the bar close is symmetric; nothing to leave out."""
    _winner_outcome(store, quoted_live=False)
    spec = {**_loser_spec(_cand("SCALP", 70)), "graded": {"r": -1.0, "resolved": True}}
    store.write(CONTESTS_KEY, [_row(spec)])

    report = audit_contests(Tracker(store, fake_client, TrackerSettings()))
    assert report["skipped"]["legacy"] == 0 and len(report["pairs"]) == 1


def test_unfillable_and_legacy_counts_are_printed(store, fake_client):
    _winner_outcome(store, quoted_live=True)
    legacy = {**_loser_spec(_cand("SCALP", 70)), "graded": {"r": 0.5, "resolved": True}}
    dropped = {**_loser_spec(_cand("SCALP", 70)), "requote": "chase_drop"}
    fixed = {**_loser_spec(_cand("SCALP", 70)), "requote": "live",
             "graded": {"r": -1.0, "resolved": True}}
    store.write(CONTESTS_KEY, [_row(legacy), _row(dropped), _row(fixed)])

    report = audit_contests(Tracker(store, fake_client, TrackerSettings()))
    assert report["skipped"]["unfillable"] == 1 and report["skipped"]["legacy"] == 1
    text = render(report)
    assert "1 could not have been entered either" in text
    assert "1 pairs recorded before 2026-10-07 left out" in text
