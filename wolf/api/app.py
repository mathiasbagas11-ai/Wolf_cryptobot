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
POST /signals/outcomes/import  merge an exported outcome log back into state
POST /scan              run one screening cycle now
POST /track             advance pending signals now
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

from wolf.app import Application, ai_status, build_application, build_deepdive
from wolf.config import Settings
from wolf.diagnose import diagnose, render_digest
from wolf.logging_setup import setup_logging
from wolf.chase_audit import grade_chase_drops, render as render_chase
from wolf.contest_audit import audit_contests, render as render_contest
from wolf.montecarlo import ledger_path, render as render_montecarlo, simulate
from wolf.whatif import compare_stop_rules, render as render_whatif


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

    def _authorised(x_api_key: str) -> bool:
        expected = app_obj.settings.api_key
        if expected:
            return x_api_key == expected
        # No key: open only while nothing outside the container can reach it.
        return not app_obj.settings.public_domain

    def require_api_key(x_api_key: str = Header(default="")) -> None:
        """Guard for every endpoint but a bare /health.

        Reads are guarded as well as writes. The ledger and the paper account
        are the owner's, and the /whatif replays cost one klines request per
        signal, so an open GET is both a leak and a way to burn the bot's rate
        limit. With a public domain and no key configured it refuses with a
        503 that names the fix, rather than serving anyone who finds the URL.
        """
        if _authorised(x_api_key):
            return
        if not app_obj.settings.api_key:
            raise HTTPException(
                status_code=503,
                detail="Service has a public domain but API_KEY is not set — set it in Railway variables",
            )
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    @api.get("/health")
    def health(probe_ai: bool = False, x_api_key: str = Header(default="")) -> dict:
        # state_dir is reported absolute, and outcomes_stored alongside it, so a
        # wiped history after a redeploy is visible instead of being mistaken for
        # a quiet week. A relative path means the container filesystem, which
        # Railway discards on every deploy unless a volume is mounted there.
        store = app_obj.store
        if not _authorised(x_api_key):
            # Liveness stays public so a dashboard or uptime check can tell a
            # dead bot from a locked one; nothing else is told to a stranger.
            return {"status": "ok", "api_key_set": bool(app_obj.settings.api_key)}
        return {
            "status": "ok",
            "api_key_set": bool(app_obj.settings.api_key),
            "state_dir": store.base_dir,
            "outcomes_stored": len(store.read("signal_outcomes", default=[]) or []),
            "telegram_enabled": app_obj.notifier.enabled,
            # enabled vs available: when they disagree, every signal abstains.
            # ?probe_ai=true spends one arbiter call to ask the provider rather
            # than the config, which is the only way to catch a key that is set
            # but rejected, or a balance that has run out.
            "ai": ai_status(app_obj, probe=probe_ai),
            "config": app_obj.settings.describe(),
        }

    @api.get("/signals/active", dependencies=[Depends(require_api_key)])
    def active_signals() -> dict:
        signals = app_obj.tracker.active_signals()
        return {"count": len(signals), "signals": [s.to_dict() for s in signals]}

    @api.get("/signals/outcomes", dependencies=[Depends(require_api_key)])
    def outcomes(limit: int = 50) -> dict:
        items = app_obj.tracker.outcomes()
        items = list(reversed(items))[: max(1, min(limit, 500))]
        return {"count": len(items), "outcomes": [s.to_dict() for s in items]}

    @api.get("/stats", dependencies=[Depends(require_api_key)])
    def stats(window_hours: Optional[float] = None) -> dict:
        return app_obj.tracker.stats(window_hours=window_hours)

    @api.get("/paper", dependencies=[Depends(require_api_key)])
    def paper() -> dict:
        if app_obj.account is None:
            return {"enabled": False}
        # The ledger line rides along so a dashboard never shows the gross
        # balance alone: it is the same pair /paper prints in Telegram.
        ledger = ledger_path(
            app_obj.tracker.outcomes(),
            risk_pct=app_obj.settings.paper_risk_pct,
            round_trip_bps=app_obj.settings.round_trip_cost_bps,
        )
        return {"enabled": True, **app_obj.account.summary(), "ledger": ledger}

    @api.get("/montecarlo", dependencies=[Depends(require_api_key)])
    def montecarlo(strategy: Optional[str] = None, format: str = "json"):
        """Day-block bootstrap of the ledger, net of cost — see wolf.montecarlo."""
        report = simulate(
            app_obj.tracker.outcomes(),
            risk_pct=app_obj.settings.paper_risk_pct,
            round_trip_bps=app_obj.settings.round_trip_cost_bps,
            strategy=strategy,
        )
        if format == "text":
            return PlainTextResponse(render_montecarlo(report))
        return report

    @api.get("/learning", dependencies=[Depends(require_api_key)])
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

    @api.post("/flow", dependencies=[Depends(require_api_key)])
    def flow_report() -> dict:
        """Render the Flow Intelligence digest now and post it to its topic.

        503 when no collector has produced data yet: the reporter reads the
        StateStore and cannot manufacture a digest out of nothing. Run the
        collectors (or wait for their scheduled jobs) first.
        """
        if app_obj.flow is None:
            raise HTTPException(status_code=503, detail="Flow reporting is disabled")
        text = app_obj.flow.build()
        if not text:
            raise HTTPException(status_code=503, detail="No collected flow data yet")
        app_obj.notifier.notify_flow(text)
        return {"posted": app_obj.notifier.enabled, "text": text}

    @api.post("/rank", dependencies=[Depends(require_api_key)])
    def conviction_ranking() -> dict:
        """Rank the live signal book now and post it to the High-Conviction topic.

        Forced, unlike the scheduled job: an explicit request is answered even
        when the ranking has not changed since the last post.
        """
        if app_obj.conviction is None:
            raise HTTPException(status_code=503, detail="Conviction ranking is disabled")
        text = app_obj.conviction.build(force=True)
        if not text:
            raise HTTPException(
                status_code=503,
                detail="Nothing to rank — too few live signals, or none worth taking",
            )
        app_obj.notifier.notify_conviction(text)
        return {"posted": app_obj.notifier.enabled, "text": text}

    @api.post("/flow/{symbol}", dependencies=[Depends(require_api_key)])
    def flow_deep_dive(symbol: str) -> dict:
        """Single-token honest deep-dive (bull vs bear), fetched on demand."""
        reporter = app_obj.deepdive or build_deepdive(app_obj.settings, app_obj.client)
        text = reporter.build_token(symbol)
        if not text:
            raise HTTPException(status_code=404, detail=f"Token '{symbol}' not found")
        app_obj.notifier.notify_flow(text)
        return {"posted": app_obj.notifier.enabled, "text": text}

    @api.get("/diagnostics", dependencies=[Depends(require_api_key)])
    def diagnostics(window_hours: Optional[float] = None, format: str = "json"):
        """Derived statistics behind a performance verdict.

        ``format=text`` returns the compact digest — a fixed-shape block small
        enough to paste whole into a conversation, carrying the verdicts and the
        evidence needed to argue with them.
        """
        diag = diagnose(
            app_obj.tracker,
            window_hours=window_hours,
            round_trip_bps=app_obj.settings.round_trip_cost_bps,
            taker_fee_bps=app_obj.settings.taker_fee_bps,
            max_cost_r=app_obj.settings.max_cost_r,
            tp1_banks_win=app_obj.settings.tracker.tp1_banks_win,
            state_dir=app_obj.settings.state_dir,
            ai_available=ai_status(app_obj)["available"],
        )
        if format == "text":
            return PlainTextResponse(render_digest(diag))
        return diag

    @api.get("/whatif/stops", dependencies=[Depends(require_api_key)])
    def whatif_stops(limit: int = 200, format: str = "text"):
        """Re-grade resolved signals under each stop-advance rule.

        Refetches the candles that decided each trade and replays it with one
        setting changed, so the two columns differ only by the rule. Costs one
        klines request per signal, which is why it is asked for rather than
        folded into the daily digest.
        """
        report = compare_stop_rules(app_obj.tracker, limit=limit)
        if format == "text":
            return PlainTextResponse(render_whatif(report))
        return {
            "error": report.get("error", ""),
            "sample": report.get("sample", 0),
            "results": [asdict(r) for r in report.get("results", [])],
        }

    @api.get("/whatif/chase", dependencies=[Depends(require_api_key)])
    def whatif_chase(limit: int = 200, overlap: float = 0.0, format: str = "text"):
        """Replay the candidates the chase gate dropped before they were logged.

        The gate rejects a market entry that has already run past the price its
        detector quoted, and it does so before ``record_signal`` — so nothing it
        drops appears in ``traded``, in a bucket, or in a strategy row. The
        drops are persisted for exactly this: each is rebuilt as the signal the
        re-quote would have written and graded over the candles that followed.

        Costs one klines request per drop, like ``/whatif/stops``, which is why
        it is asked for rather than folded into the digest. ``overlap`` charges
        the comparison an independence discount; 0 reads the live book's own
        mean_open, matching what the digest charges everything else.
        """
        if overlap <= 0:
            diag = diagnose(app_obj.tracker, window_hours=168)
            overlap = float(diag.get("concurrency", {}).get("mean_open") or 1.0) or 1.0
        report = grade_chase_drops(
            app_obj.tracker, ladder=app_obj.settings.ladder,
            limit=limit, overlap=overlap,
        )
        if format == "text":
            return PlainTextResponse(render_chase(report))
        return report

    @api.get("/whatif/contest", dependencies=[Depends(require_api_key)])
    def whatif_contest(limit: int = 300, overlap: float = 0.0, format: str = "text"):
        """Pair each displaced candidate against the winner that beat it.

        ``_best_candidate`` keeps one candidate per symbol by ``max(score)``,
        and the scores it compares are not on a common scale — each detector's
        gates guarantee a different floor. This reads the losers back against
        what the winner actually returned, on the same symbol and bar, so the
        market move cancels. Unlike the chase audit this needs no refetching:
        the losers are graded on a schedule and their verdicts are stored.
        """
        if overlap <= 0:
            diag = diagnose(app_obj.tracker, window_hours=168)
            overlap = float(diag.get("concurrency", {}).get("mean_open") or 1.0) or 1.0
        report = audit_contests(app_obj.tracker, limit=limit, overlap=overlap)
        if format == "text":
            return PlainTextResponse(render_contest(report))
        return report

    @api.post("/signals/outcomes/rebank", dependencies=[Depends(require_api_key)])
    def rebank(dry_run: bool = True):
        """Re-book timeout outcomes whose banked rungs were never counted.

        Until 2026-09-18 a position that timed out was priced entirely at the
        price on the clock, as though the slice already sold at TP1 were still
        open — the two other exit paths blended, this one did not. Defaults to
        a dry run: a ledger is not something to rewrite before looking at what
        would move. Idempotent, so a second run finds nothing.

        The paper balance is replayed from the corrected log rather than
        patched, because it compounds. ``learning_memory`` is left alone; see
        the module docstring for what that costs.
        """
        from wolf.rebank import rebank_outcomes

        return rebank_outcomes(
            app_obj.store, app_obj.settings.tracker,
            start_balance=app_obj.settings.paper_start_balance,
            risk_pct=app_obj.settings.paper_risk_pct,
            dry_run=dry_run,
        )

    @api.post("/signals/outcomes/import", dependencies=[Depends(require_api_key)])
    def import_outcomes(payload: Any = Body(...)) -> dict:
        """Merge a previously exported outcome log back into state.

        Restores the sample after a redeploy has wiped an unmounted state dir.
        Merging is by signal ``id`` and never overwrites a record already
        present, so re-running an import is a no-op rather than a duplication —
        and a stale export cannot clobber fresher outcomes.

        Accepts what ``GET /signals/outcomes`` returns: either the whole
        response object or a bare list.
        """
        raw = payload.get("outcomes", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="Expected a list of outcomes")

        incoming = [d for d in raw if isinstance(d, dict) and d.get("id")]
        skipped = len(raw) - len(incoming)

        def _merge(current):
            existing = list(current or [])
            seen = {d.get("id") for d in existing if isinstance(d, dict)}
            added = [d for d in incoming if d["id"] not in seen]
            # Oldest-first ordering is what the outcome log and the max_outcomes
            # trim both assume; restored records are older than whatever the
            # fresh container has already booked.
            merged = added + existing
            return merged[-app_obj.settings.tracker.max_outcomes:]

        before = len(app_obj.store.read("signal_outcomes", default=[]) or [])
        merged = app_obj.store.update("signal_outcomes", _merge, default=[])
        return {
            "imported": len(merged) - before,
            "skipped_without_id": skipped,
            "total_stored": len(merged),
        }

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
