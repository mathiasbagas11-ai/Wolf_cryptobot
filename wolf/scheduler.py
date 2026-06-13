"""Background scheduling.

Wraps APScheduler to run the two periodic jobs of the bot:

* **track** — advance pending signals (default every 5 min)
* **scan**  — run the screening cycle (default every 10 min)

Jobs are configured with ``max_instances=1`` and ``coalesce=True`` so a slow
cycle can never overlap itself — combined with the locked state store this keeps
persistence race-free.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from wolf.app import Application

log = logging.getLogger("wolf.scheduler")


def _guarded(fn, label: str):
    def wrapper() -> None:
        try:
            fn()
        except Exception:  # a job crash must not kill the scheduler thread
            log.exception("Scheduled job '%s' failed", label)

    return wrapper


def build_scheduler(app: Application) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _guarded(app.tracker.check_pending, "track"),
        "interval",
        minutes=app.settings.tracker_interval_min,
        id="track",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )
    scheduler.add_job(
        _guarded(app.screener.run_cycle, "scan"),
        "interval",
        minutes=app.settings.screener_interval_min,
        id="scan",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )
    return scheduler
