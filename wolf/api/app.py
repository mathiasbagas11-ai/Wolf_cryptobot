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

from fastapi import Body, Depends, FastAPI, Header, HTTPException

from wolf.app import Application, build_application, build_flow_reporter
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

    def require_api_key(x_api_key: str = Header(default="")) -> None:
        """Guard for state-mutating endpoints. No-op when no key is configured."""
        expected = app_obj.settings.api_key
        if expected and x_api_key != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

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

    @api.get("/paper")
    def paper() -> dict:
        if app_obj.account is None:
            return {"enabled": False}
        return {"enabled": True, **app_obj.account.summary()}

    @api.get("/learning")
    def learning() -> dict:
        if app_obj.learning is None:
            return {"enabled": False}
        return {"enabled": True, **app_obj.learning.snapshot()}

    @api.post("/backtest", dependencies=[Depends(require_api_key)])
    def backtest(payload: dict = Body(default={})) -> dict:
        if app_obj.backtest is None:
            raise HTTPException(status_code=404, detail="Backtest not available")
        symbols = payload.get("symbols") or app_obj.screener.current_universe()
        result = app_obj.backtest.run(symbols)
        return {"total_trades": result["total_trades"], "by_strategy": result["by_strategy"]}

    @api.post("/scan", dependencies=[Depends(require_api_key)])
    def scan() -> dict:
        recorded = app_obj.screener.run_cycle()
        return {"recorded": len(recorded), "signals": [s.to_dict() for s in recorded]}

    @api.post("/track", dependencies=[Depends(require_api_key)])
    def track() -> dict:
        resolved = app_obj.tracker.check_pending()
        return {"resolved": len(resolved), "signals": [s.to_dict() for s in resolved]}

    def _flow_reporter():
        """Reuse the scheduled reporter, or build one on demand if flow is off."""
        return app_obj.flow or build_flow_reporter(app_obj.settings, app_obj.client)

    @api.post("/flow", dependencies=[Depends(require_api_key)])
    def flow_report() -> dict:
        """Build the flow-intelligence brief now and post it to the News topic."""
        text = _flow_reporter().build()
        if not text:
            raise HTTPException(status_code=503, detail="No flow data available")
        app_obj.notifier.notify_flow(text)
        return {"posted": app_obj.notifier.enabled, "text": text}

    @api.post("/flow/{symbol}", dependencies=[Depends(require_api_key)])
    def flow_deep_dive(symbol: str) -> dict:
        """Single-token contrarian deep-dive (bull vs bear) → News topic."""
        text = _flow_reporter().build_token(symbol)
        if not text:
            raise HTTPException(status_code=404, detail=f"Token '{symbol}' not found")
        app_obj.notifier.notify_flow(text)
        return {"posted": app_obj.notifier.enabled, "text": text}

    @api.post("/signals", dependencies=[Depends(require_api_key)])
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
