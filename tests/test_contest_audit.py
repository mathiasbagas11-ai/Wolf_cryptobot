"""Tests for grading the candidates that lost the per-symbol contest.

``_best_candidate`` keeps one candidate per symbol by ``max(score)`` and
discards the rest without a record — the fifth instance of this project's
self-blinding shape, and a costly one because the scores compared are not on a
common scale: each detector's gates guarantee a different floor. Recording the
losers makes the question paired, which is the only reason it is affordable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.config import TrackerSettings
from wolf.contest_audit import (
    CONTESTS_KEY, audit_contests, grade_pending_contests, render,
)
from wolf.models import Candle, Signal, Status
from wolf.tracker import OUTCOMES_KEY, Tracker

_BAR_MS = 900_000


class _PathClient:
    def __init__(self, paths: dict) -> None:
        self.paths = paths

    def get_klines(self, symbol, interval="15m", limit=100):
        closes = self.paths.get(symbol)
        if not closes:
            return []
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        start = now - len(closes) * _BAR_MS
        out = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            out.append(Candle(time=start + i * _BAR_MS, open=o,
                              high=max(o, c) * 1.001, low=min(o, c) * 0.999,
                              close=c, volume=1000.0))
        return out[-limit:]


# The path must span the contest's age or the grader fails for the very reason
# the scheduling exists: 50h needs 200 bars of 15m, not 120. Same shape as the
# production fault this module was written to avoid.
def _rising(n=260, start=100.0, step=0.18):
    return [start + i * step for i in range(n)]


def _falling(n=260, start=100.0, step=0.15):
    return [start - i * step for i in range(n)]


def _loser(strategy="PREPUMP", score=88, entry=100.0, sl=97.0):
    return {
        "strategy": strategy, "signal_type": strategy, "direction": "LONG",
        "score": score, "entry_price": entry, "sl": sl, "tp": entry + 9.0,
        "tps": [
            {"level": 1, "price": entry + 3.0, "allocation": 0.5, "r_multiple": 1.0},
            {"level": 2, "price": entry + 6.0, "allocation": 0.3, "r_multiple": 2.0},
            {"level": 3, "price": entry + 9.0, "allocation": 0.2, "r_multiple": 3.0},
        ],
        "timeframe": "1h", "entry_mode": "MOMENTUM_NOW",
    }


def _contest(symbol="UP", signal_id="sig-1", hours_ago=50.0, losers=None):
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "at": at.isoformat(timespec="seconds"), "symbol": symbol,
        "signal_id": signal_id,
        "winner": {"strategy": "MOMENTUM", "score": 100, "direction": "LONG"},
        "losers": losers if losers is not None else [_loser()],
    }


def _winner_outcome(signal_id="sig-1", symbol="UP", r=-1.0, hours_ago=50.0):
    start = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return Signal(
        id=signal_id, symbol=symbol, signal_type="SCREENER", direction="LONG",
        entry_price=100.0, tp=109.0, sl=97.0, strategy="MOMENTUM",
        tp_ladder=[{"level": 1, "price": 103.0}],
        status=(Status.TP_HIT if r > 0 else Status.SL_HIT).value,
        pnl_pct=r * 3.0, r_multiple=r,
        activated_at=start.isoformat(),
        exit_time=(start + timedelta(hours=1)).isoformat(),
        resolved_at=(start + timedelta(hours=1)).isoformat(),
    ).to_dict()


def _tracker(store, paths=None) -> Tracker:
    return Tracker(store, _PathClient(paths or {}), TrackerSettings())


# ── Grading ──────────────────────────────────────────────────────────────

def test_a_displaced_candidate_is_graded_on_its_own_geometry(store):
    """Its detector chose the entry, stop and ladder. Rebuilding them from a
    policy would grade a trade that detector never proposed."""
    store.write(CONTESTS_KEY, [_contest()])
    assert grade_pending_contests(_tracker(store, {"UP": _rising()}))["graded"] == 1

    verdict = store.read(CONTESTS_KEY)[0]["losers"][0]["graded"]
    assert verdict["r"] > 0 and verdict["resolved"] is True


def test_a_loser_is_graded_once_and_never_recomputed(store):
    store.write(CONTESTS_KEY, [_contest()])
    tracker = _tracker(store, {"UP": _rising()})
    assert grade_pending_contests(tracker)["graded"] == 1
    assert grade_pending_contests(tracker)["graded"] == 0


def test_a_contest_too_young_to_have_settled_is_left_alone(store):
    """PREPUMP times out at 48h, so a two-hour-old contest has not run its
    course and grading it would score a trade mid-flight."""
    store.write(CONTESTS_KEY, [_contest(hours_ago=2.0)])
    assert grade_pending_contests(_tracker(store, {"UP": _rising()}))["graded"] == 0


def test_a_transient_fetch_failure_leaves_the_loser_pending(store):
    store.write(CONTESTS_KEY, [_contest(symbol="LATER")])
    assert grade_pending_contests(_tracker(store, {}))["failed"] == 1
    assert "graded" not in store.read(CONTESTS_KEY)[0]["losers"][0]
    assert grade_pending_contests(_tracker(store, {"LATER": _rising()}))["graded"] == 1


# ── Pairing ──────────────────────────────────────────────────────────────

def test_the_winner_is_paired_against_what_the_loser_would_have_returned(store):
    """The whole point: same symbol, same bar, so the market move cancels."""
    store.write(OUTCOMES_KEY, [_winner_outcome(r=-1.0)])
    store.write(CONTESTS_KEY, [_contest()])
    tracker = _tracker(store, {"UP": _rising()})
    grade_pending_contests(tracker)

    report = audit_contests(tracker, overlap=1.0)
    assert len(report["pairs"]) == 1
    pair = report["pairs"][0]
    assert pair["winner"] == "MOMENTUM" and pair["loser"] == "PREPUMP"
    assert pair["winner_r"] == -1.0 and pair["loser_r"] > 0
    assert "MOMENTUM>PREPUMP" in report["by_matchup"]


def test_a_contest_whose_winner_never_resolved_is_not_paired(store):
    """Grading a settled loser against an open winner would compare the two on
    different amounts of information."""
    store.write(OUTCOMES_KEY, [])
    store.write(CONTESTS_KEY, [_contest()])
    tracker = _tracker(store, {"UP": _rising()})
    grade_pending_contests(tracker)

    report = audit_contests(tracker)
    assert report["pairs"] == []
    assert report["skipped"]["winner_unresolved"] == 1


def test_the_paired_difference_is_charged_the_overlap_discount(store):
    """Paired or not, concurrent positions are not independent observations."""
    outcomes, contests = [], []
    for i, (wr, path) in enumerate([(-1.0, "UP"), (+1.2, "UP"), (-1.0, "DOWN"), (+0.4, "DOWN")]):
        outcomes.append(_winner_outcome(signal_id=f"s{i}", symbol=path, r=wr,
                                        hours_ago=50.0 + i))
        contests.append(_contest(symbol=path, signal_id=f"s{i}", hours_ago=50.0 + i))
    store.write(OUTCOMES_KEY, outcomes)
    store.write(CONTESTS_KEY, contests)
    tracker = _tracker(store, {"UP": _rising(), "DOWN": _falling()})
    grade_pending_contests(tracker)

    report = audit_contests(tracker, overlap=4.0)
    stats = report["overall"]
    assert stats is not None and stats["n"] == 4
    assert abs(stats["t"]) < abs(stats["t_nominal"])
    assert "paired" in render(report)


def test_nothing_recorded_says_so(store):
    report = audit_contests(_tracker(store))
    assert report["pairs"] == []
    assert "no score contests recorded" in render(report)
