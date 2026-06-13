"""Logging configuration.

A single :func:`setup_logging` call configures the root logger with a concise,
timestamped format. Every module obtains its logger via ``logging.getLogger``
so log lines are attributable to a component — important because the new design
favours ``log.exception(...)`` over silent ``except: pass`` blocks.
"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)
    # Third-party libraries are noisy at INFO; keep them at WARNING.
    for noisy in ("urllib3", "apscheduler", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
