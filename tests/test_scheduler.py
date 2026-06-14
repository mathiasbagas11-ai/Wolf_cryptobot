"""Scheduler must add jobs in a runnable (non-paused) state."""

from __future__ import annotations

from types import SimpleNamespace

from wolf.scheduler import build_scheduler


class _Settings(SimpleNamespace):
    pass


def _app():
    notifier = SimpleNamespace(enabled=False)
    settings = _Settings(
        tracker_interval_min=5, screener_interval_min=10, stats_report_hours=24,
        news=SimpleNamespace(interval_min=30),
        reports=SimpleNamespace(
            majors_interval_min=60, radar_interval_min=30,
            pulse_interval_min=30, whale_interval_min=5,
        ),
    )
    return SimpleNamespace(
        settings=settings, notifier=notifier,
        tracker=SimpleNamespace(check_pending=lambda: None, stats=lambda: {}),
        screener=SimpleNamespace(run_cycle=lambda: None),
        news=None, majors=None, radar=None, pulse=None, whale=None,
    )


def test_core_jobs_have_a_next_run_time():
    sched = build_scheduler(_app())
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "track" in jobs and "scan" in jobs
    # next_run_time=None would mean PAUSED (never fires) — the bug we fixed.
    assert jobs["track"].next_run_time is not None
    assert jobs["scan"].next_run_time is not None
