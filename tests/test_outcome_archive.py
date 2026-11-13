"""Tests for the permanent outcome archive behind the capped log.

The outcome log keeps only its newest ``max_outcomes`` rows, so any window
longer than it used to read only what had survived the trim, with nothing on
the card to say so. Every resolved outcome is now also appended to a log that
is never trimmed, and the diagnostic reads the two together.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from wolf.config import TrackerSettings
from wolf.diagnose import diagnose, render_digest
from wolf.models import Signal, Status
from wolf.tracker import ARCHIVE_KEY, OUTCOMES_KEY, Tracker


def _row(i: int, *, r: float = 1.0, days_ago: float = 0.0) -> Signal:
    end = datetime.now(timezone.utc) - timedelta(days=days_ago)
    start = end - timedelta(hours=2)
    return Signal(
        id=f"S{i}", symbol=f"S{i}USDT", signal_type="SCREENER", direction="LONG",
        entry_price=100.0, tp=105.0, sl=99.0, strategy="SCALP",
        status=Status.TP_HIT.value if r > 0 else Status.SL_HIT.value,
        pnl_pct=r, r_multiple=r, created_at=start.isoformat(),
        activated_at=start.isoformat(), exit_time=end.isoformat(),
        resolved_at=end.isoformat(),
    )


def _tracker(store, fake_client, cap: int) -> Tracker:
    return Tracker(store, fake_client, TrackerSettings(max_outcomes=cap))


def test_a_row_trimmed_from_the_capped_log_survives_in_the_history(store, fake_client):
    t = _tracker(store, fake_client, cap=3)
    for i in range(5):
        t._append_outcome(_row(i, days_ago=5 - i))
    assert [o.id for o in t.outcomes()] == ["S2", "S3", "S4"]
    assert [o.id for o in t.history()] == ["S0", "S1", "S2", "S3", "S4"]


def test_the_archive_starts_from_whatever_the_log_still_holds(store, fake_client):
    """Rows trimmed before the archive existed are gone; the rest are kept."""
    store.write(OUTCOMES_KEY, [_row(i, days_ago=10 - i).to_dict() for i in range(3)])
    t = _tracker(store, fake_client, cap=3)
    for i in range(3, 6):
        t._append_outcome(_row(i, days_ago=10 - i))
    assert len(t.outcomes()) == 3
    assert [o.id for o in t.history()] == [f"S{i}" for i in range(6)]


def test_the_log_copy_of_a_row_wins_over_the_archive_copy(store, fake_client):
    """A correction (such as a re-booking) is written to the log, so the log's
    version is the one to trust when both hold the same row."""
    t = _tracker(store, fake_client, cap=10)
    t._append_outcome(_row(1, r=2.0))
    corrected = replace(_row(1, r=2.0), r_multiple=1.5)
    store.write(OUTCOMES_KEY, [corrected.to_dict()])
    rows = t.history()
    assert len(rows) == 1 and rows[0].r_multiple == 1.5


def test_a_torn_line_in_the_archive_is_skipped_not_fatal(store, fake_client):
    t = _tracker(store, fake_client, cap=1)
    t._append_outcome(_row(1, days_ago=2))
    with open(store._lines_path(ARCHIVE_KEY), "a", encoding="utf-8") as fh:
        fh.write('{"id": "S9", "symbol": \n')
    t._append_outcome(_row(2, days_ago=1))
    assert [o.id for o in t.history()] == ["S1", "S2"]


def test_a_failing_archive_never_stops_the_ledger_booking_the_outcome(
    store, fake_client, monkeypatch
):
    t = _tracker(store, fake_client, cap=5)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "append_lines", boom)
    t._append_outcome(_row(1))
    assert [o.id for o in t.outcomes()] == ["S1"]


def test_a_long_diag_window_reaches_past_the_capped_log(store, fake_client):
    """The bug this exists for: a 30-day window over a 25-day log read 25 days."""
    t = _tracker(store, fake_client, cap=4)
    for i in range(10):
        t._append_outcome(_row(i, r=1.0 if i % 2 else -1.0, days_ago=28 - 3 * i))
    diag = diagnose(t, window_hours=720)
    assert diag["overall"]["n"] == 10
    assert diag["history"]["rows"] == 10 and diag["history"]["log_rows"] == 4
    assert "history  10 resolved rows on record" in render_digest(diag)
