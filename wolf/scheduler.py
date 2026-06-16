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

    # Periodic performance summary to Telegram (0 hours disables it).
    stats_hours = app.settings.stats_report_hours
    if stats_hours > 0 and app.notifier.enabled:
        scheduler.add_job(
            _guarded(lambda: app.notifier.notify_stats(app.tracker.stats()), "stats"),
            "interval",
            hours=stats_hours,
            id="stats",
            max_instances=1,
            coalesce=True,
            next_run_time=None,
        )

    # Crypto news: post fresh headlines to the News topic.
    if app.news is not None and app.notifier.enabled:
        scheduler.add_job(
            _guarded(lambda: app.notifier.notify_news(app.news.fetch_new()), "news"),
            "interval",
            minutes=app.settings.news.interval_min,
            id="news",
            max_instances=1,
            coalesce=True,
            next_run_time=None,
        )

    # Periodic market reports, each to its own topic.
    r = app.settings.reports
    _add_report_job(scheduler, app.notifier.enabled and app.majors is not None,
                    "majors", r.majors_interval_min,
                    lambda: app.notifier.notify_majors(app.majors.build()))
    _add_report_job(scheduler, app.notifier.enabled and app.radar is not None,
                    "radar", r.radar_interval_min,
                    lambda: app.notifier.notify_radar(app.radar.build()))
    _add_report_job(scheduler, app.notifier.enabled and app.pulse is not None,
                    "pulse", r.pulse_interval_min,
                    lambda: app.notifier.notify_pulse(app.pulse.build()))
    _add_report_job(scheduler, app.notifier.enabled and app.whale is not None,
                    "whale", r.whale_interval_min,
                    lambda: app.notifier.notify_whale(app.whale.build()))

    # Flow-intelligence brief (Nansen-style thread) → News topic.
    _add_report_job(scheduler, app.notifier.enabled and app.flow is not None,
                    "flow", app.settings.flow.interval_min,
                    lambda: app.notifier.notify_flow(app.flow.build()))
    return scheduler


def _add_report_job(scheduler, enabled: bool, job_id: str, minutes: int, fn) -> None:
    if not enabled:
        return
    scheduler.add_job(
        _guarded(fn, job_id),
        "interval",
        minutes=minutes,
        id=job_id,
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )
