"""Worker entrypoint.

Boots the application: starts the background scheduler (tracking + screening
jobs) and serves the REST API with uvicorn in the foreground. Designed to run as
a single long-lived process (Railway worker / `Procfile`).
"""

from __future__ import annotations

import logging

import uvicorn

from wolf.api import create_app
from wolf.app import build_application
from wolf.config import Settings
from wolf.logging_setup import setup_logging
from wolf.scheduler import build_scheduler

log = logging.getLogger("wolf.main")


def _risk_gates_label(risk) -> str:
    """One-line summary of the active risk gates for the startup message."""
    parts = []
    if risk.regime_filter_enabled:
        parts.append(f"regime({risk.regime_symbol})")
    parts.append(f"drawdown≥{risk.drawdown_pause_pct:.0f}%")
    parts.append(f"autopause<{risk.autopause_min_win_rate:.0f}%/{risk.autopause_min_trades}")
    return " · ".join(parts)


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    log.info("Starting Wolf Crypto Tracker")

    application = build_application(settings)
    api = create_app(application)

    scheduler = build_scheduler(application)
    scheduler.start()
    log.info(
        "Scheduler started (track=%dm, scan=%dm)",
        settings.tracker_interval_min,
        settings.screener_interval_min,
    )

    # Validate every configured topic up front so a wrong/stale *_THREAD_ID is
    # reported once (with its label) instead of failing silently on each post.
    try:
        validation = application.notifier.validate_threads()
        application.notifier.report_thread_validation(validation)
    except Exception:
        log.exception("Telegram topic validation failed")

    # Announce online to Telegram so the channel confirms the bot is up (and
    # surfaces any chat/topic misconfiguration in the logs immediately).
    application.notifier.notify_startup({
        "sources": application.client.source_names,
        "detectors": application.screener.detector_names,
        "universe": application.screener.universe_size,
        "scan_min": settings.screener_interval_min,
        "track_min": settings.tracker_interval_min,
        "ai": settings.ai.enabled,
        "ai_mode": "MONITOR" if settings.ai.enabled else "OFF",
        "risk_gates": _risk_gates_label(settings.risk),
    })

    # Run an initial tracking pass so restarts resolve overdue signals promptly.
    try:
        application.tracker.check_pending()
    except Exception:
        log.exception("Initial tracking pass failed")

    try:
        uvicorn.run(api, host=settings.api_host, port=settings.api_port, log_level=settings.log_level.lower())
    finally:
        scheduler.shutdown(wait=False)
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
