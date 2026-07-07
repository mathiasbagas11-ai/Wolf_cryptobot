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
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from wolf.app import Application

log = logging.getLogger("wolf.scheduler")


def _soon() -> datetime:
    """First-fire time: run each job right away on boot, then on its interval.

    Passing ``next_run_time=None`` to APScheduler would add the job *paused* (it
    never fires) — the bug that left every room silent. Using ``now`` schedules
    an immediate first run so reports/tracking start without waiting a full
    interval.
    """
    return datetime.now(timezone.utc)


def _guarded(fn, label: str):
    def wrapper() -> None:
        try:
            fn()
        except Exception:  # a job crash must not kill the scheduler thread
            log.exception("Scheduled job '%s' failed", label)

    return wrapper


def build_scheduler(app: Application) -> BackgroundScheduler:
    # A generous misfire grace so the immediate first run (next_run_time=now) is
    # not skipped if start() lags a second or two behind build — otherwise the
    # boot-time report would be silently dropped as a "misfire".
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"misfire_grace_time": 300, "coalesce": True},
    )
    scheduler.add_job(
        _guarded(app.tracker.check_pending, "track"),
        "interval",
        minutes=app.settings.tracker_interval_min,
        id="track",
        max_instances=1,
        coalesce=True,
        next_run_time=_soon(),
    )
    scheduler.add_job(
        _guarded(app.screener.run_cycle, "scan"),
        "interval",
        minutes=app.settings.screener_interval_min,
        id="scan",
        max_instances=1,
        coalesce=True,
        next_run_time=_soon(),
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
            next_run_time=_soon(),
        )

    # Crypto news: fetch fresh headlines and auto-post to the News topic. When a
    # synthesizer is configured, the batch is condensed into one AI brief;
    # otherwise the plain card is posted.
    if app.news is not None and app.notifier.enabled:
        scheduler.add_job(
            _guarded(lambda: _post_news(app), "news"),
            "interval",
            minutes=app.settings.news.interval_min,
            id="news",
            max_instances=1,
            coalesce=True,
            next_run_time=_soon(),
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
    if getattr(app, "flow", None) is not None:
        _add_report_job(scheduler, app.notifier.enabled, "flow",
                        app.settings.flow.interval_min,
                        lambda: app.notifier.notify_flow(app.flow.build()))
    return scheduler


def _post_news(app: Application) -> None:
    """One news cycle: fetch fresh, synthesise if possible, else post the card.
    When a news_scanner is configured, also generate and announce NEWS signals."""
    items = app.news.fetch_new()
    if not items:
        return

    scanner = getattr(app, "news_scanner", None)
    if scanner is not None:
        candidates = scanner.scan(items)
        for candidate in candidates:
            signal = app.tracker.record_signal(
                symbol=candidate.symbol,
                signal_type=candidate.signal_type,
                direction=candidate.direction,
                entry_price=candidate.entry_price,
                tp=candidate.tp,
                sl=candidate.sl,
                score=candidate.score,
                confluence_level=candidate.confluence_level,
                reasons=candidate.reasons,
                strategy=candidate.strategy,
                entry_mode=candidate.entry_mode,
                tps=candidate.tps,
            )
            if signal is not None:
                app.notifier.announce_signal(signal)

    synth = getattr(app, "news_synth", None)
    if synth is not None and synth.available:
        brief = synth.build(items)
        if brief:
            app.notifier.notify_news_digest(brief)
            return
    app.notifier.notify_news(items)


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
        next_run_time=_soon(),
    )
