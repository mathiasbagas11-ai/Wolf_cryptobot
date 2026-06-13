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
