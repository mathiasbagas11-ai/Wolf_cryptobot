"""REST API for the tracker.

A small FastAPI app exposing read access to tracked signals and stats, plus
manual scan/check triggers. The :class:`~wolf.app.Application` is created once at
startup and shared via dependency injection — no globals, no per-request wiring.

Endpoints
---------
GET  /health            liveness + redacted config
GET  /signals/active    currently pending/active signals
GET  /signals/outcomes  resolved outcomes (most recent first)
GET  /stats             aggregate win-rate / PnL stats
POST /scan              run one screening cycle now
POST /track             advance pending signals now
"""

from __future__ import annotations

from typing import Optional

from fastapi import Body, FastAPI

from wolf.app import Application, build_application
from wolf.config import Settings
from wolf.logging_setup import setup_logging


def create_app(application: Optional[Application] = None) -> FastAPI:
    settings = application.settings if application else Settings.from_env()
    setup_logging(settings.log_level)
    app_obj = application or build_application(settings)

    api = FastAPI(
        title="Wolf Crypto Tracker",
        version="1.0.0",
        description="Signal tracking bot — lifecycle tracking, screening and stats.",
    )
    api.state.application = app_obj

    @api.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "telegram_enabled": app_obj.notifier.enabled,
            "config": app_obj.settings.describe(),
        }

    @api.get("/signals/active")
    def active_signals() -> dict:
        signals = app_obj.tracker.active_signals()
        return {"count": len(signals), "signals": [s.to_dict() for s in signals]}

    @api.get("/signals/outcomes")
    def outcomes(limit: int = 50) -> dict:
        items = app_obj.tracker.outcomes()
        items = list(reversed(items))[: max(1, min(limit, 500))]
        return {"count": len(items), "outcomes": [s.to_dict() for s in items]}

    @api.get("/stats")
    def stats() -> dict:
        return app_obj.tracker.stats()

    @api.post("/scan")
    def scan() -> dict:
        recorded = app_obj.screener.run_cycle()
        return {"recorded": len(recorded), "signals": [s.to_dict() for s in recorded]}

    @api.post("/track")
    def track() -> dict:
        resolved = app_obj.tracker.check_pending()
        return {"resolved": len(resolved), "signals": [s.to_dict() for s in resolved]}

    @api.post("/signals")
    def record_manual(payload: dict = Body(...)) -> dict:
        """Manually record a signal (e.g. from an external strategy)."""
        signal = app_obj.tracker.record_signal(
            symbol=payload["symbol"],
            signal_type=payload.get("signal_type", "SCREENER"),
            direction=payload["direction"],
            entry_price=float(payload["entry_price"]),
            tp=float(payload["tp"]),
            sl=float(payload["sl"]),
            score=int(payload.get("score", 0)),
            confluence_level=payload.get("confluence_level", ""),
            reasons=payload.get("reasons", []),
            strategy=payload.get("strategy", "MANUAL"),
            entry_mode=payload.get("entry_mode", "RETEST_WAIT"),
            tps=payload.get("tps"),
        )
        if signal is None:
            return {"recorded": False, "reason": "rejected_or_duplicate"}
        return {"recorded": True, "signal": signal.to_dict()}

    return api
