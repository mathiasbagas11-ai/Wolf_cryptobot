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

from wolf.app import Application, ai_status
from wolf.diagnose import diagnose, render_digest
from wolf.reports import build_coordination_alerts

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


def _report_stats(app: Application, window_hours: int) -> None:
    """Send the period's performance card, then the diagnostic digest.

    Two messages by design: the card is read at a glance, the digest is copied
    whole into an analysis. A failure to build the digest must not cost you the
    card, so it is guarded separately.
    """
    app.notifier.notify_stats(
        app.tracker.stats(window_hours=window_hours),
        app.tracker.stats(),
    )
    try:
        app.notifier.notify_diagnostics(render_digest(diagnose(
            app.tracker,
            window_hours=window_hours,
            round_trip_bps=app.settings.round_trip_cost_bps,
            taker_fee_bps=app.settings.taker_fee_bps,
            max_cost_r=app.settings.max_cost_r,
            tp1_banks_win=app.settings.tracker.tp1_banks_win,
            state_dir=app.settings.state_dir,
            ai_available=ai_status(app)["available"],
        )))
    except Exception:
        log.exception("Diagnostics digest failed — performance card was still sent")


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
            # Report the period just elapsed, with all-time as a second line.
            # Cumulative-only meant each message blended every day since startup,
            # so a run that was deteriorating still looked healthy.
            _guarded(lambda: _report_stats(app, stats_hours), "stats"),
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

    # On-chain collectors. Each writes a snapshot to the StateStore; the Flow
    # Intelligence digest and the per-symbol signal context both read from
    # there, so one fetch serves both and they cannot disagree. Collector jobs
    # are independent of the notifier: they must keep running (and keep the
    # signal gates fed) even when Telegram is off.
    o = app.settings.onchain
    _add_collector_job(scheduler, app.valuation_collector, "onchain_collect",
                       o.valuation_interval_min,
                       lambda: app.valuation_collector.collect(_valuation_universe(app)))
    _add_collector_job(scheduler, app.whale_collector, "whale_hl_collect",
                       o.whale_interval_min, lambda: _scan_whales(app))
    _add_collector_job(scheduler, app.premium_collector, "coinbase_premium_collect",
                       o.premium_interval_min, lambda: app.premium_collector.collect())
    _add_collector_job(scheduler, app.macro_collector, "flow_macro_collect",
                       o.macro_interval_min, lambda: app.macro_collector.collect())

    # Flow Intelligence digest → its own topic. Pure rendering of what the
    # collectors already wrote, so the cadence costs nothing but a message.
    if getattr(app, "flow", None) is not None:
        _add_report_job(scheduler, app.notifier.enabled, "flow_report",
                        app.settings.flow.interval_min,
                        lambda: app.notifier.notify_flow(app.flow.build()))

    # 🏆 Conviction ranking → the High-Conviction topic. Reads the tracker's
    # own live book, so like the flow digest it costs nothing but one message
    # (and, when the AI layer is on, one LLM call for the whole book).
    if getattr(app, "conviction", None) is not None:
        _add_report_job(scheduler, app.notifier.enabled, "conviction",
                        app.settings.conviction.interval_min,
                        lambda: app.notifier.notify_conviction(app.conviction.build()))

    # Daily backfill of anomaly paper-log outcomes (7d/14d/30d % change).
    anomaly = getattr(app, "anomaly", None)
    if anomaly is not None:
        hours = app.settings.anomaly.backfill_interval_hours
        if hours > 0:
            scheduler.add_job(
                _guarded(anomaly.run_backfill, "anomaly_backfill"),
                "interval",
                hours=hours,
                id="anomaly_backfill",
                max_instances=1,
                coalesce=True,
                next_run_time=_soon(),
            )
    return scheduler


def _scan_whales(app: Application) -> None:
    """One whale scan, then alert the whale room about any coordinated entry.

    Two outputs from one scan, and they are deliberately different in kind: the
    snapshot feeds the signal gate and the periodic digest, while the alert is
    an event — it fires when several wallets pile in, and stays silent
    otherwise. The collector's per-coin cooldown means a build-up unfolding
    across scans is announced once, not every ten minutes.

    A Telegram failure must not cost the snapshot, which the gates depend on,
    so the scan is completed and persisted before anything is sent.
    """
    doc = app.whale_collector.scan()
    if not app.notifier.enabled or not app.settings.onchain.whale_alert_enabled:
        return
    for alert in build_coordination_alerts(doc, tz=app.settings.timezone):
        app.notifier.notify_whale(alert)


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


def _add_collector_job(scheduler, collector, job_id: str, minutes: int, fn) -> None:
    """Schedule a collector, skipping it when that collector is disabled.

    Same discipline as the report jobs — ``max_instances=1`` and ``coalesce``,
    so a slow fetch can never overlap itself and leave two writers racing for
    one StateStore key.
    """
    if collector is None:
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


def _valuation_universe(app: Application) -> list[str]:
    """Symbols the valuation collector refreshes this run.

    Capped, because the universe is dynamic and CoinGecko's key-less API is not:
    an unbounded top-N list would spend the rate-limit window on the tail and
    starve the majors that most signals are actually about. The scan universe is
    already ordered with the core majors first, so the cap keeps what matters.
    """
    limit = app.settings.onchain.valuation_max_symbols
    try:
        symbols = app.screener.current_universe()
    except Exception:  # a universe hiccup must not kill the collector job
        log.warning("Universe lookup failed for valuation collect", exc_info=True)
        return []
    return list(symbols)[:limit]
