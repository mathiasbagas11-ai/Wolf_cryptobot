"""Worker entrypoint.

Boots the application: starts the background scheduler (tracking + screening
jobs) and serves the REST API with uvicorn in the foreground. Designed to run as
a single long-lived process (Railway worker / `Procfile`).
"""

from __future__ import annotations

import logging

import uvicorn

from wolf.api import create_app
from wolf.app import ai_status, build_application
from wolf.config import Settings, state_is_persistent
from wolf.logging_setup import setup_logging
from wolf.notify.poller import TelegramPoller
from wolf.scheduler import build_scheduler

log = logging.getLogger("wolf.main")


def _risk_gates_label(settings) -> str:
    """One-line summary of the active risk gates for the startup message.

    Every gate that decides whether a signal is emitted belongs here, and the
    cost gate belongs here most of all: it is the one with the largest effect
    on volume, and the only way to tell it is set is to read the number it was
    set to. A ``MAX_COST_R`` that never reached the container leaves the code
    default in force, the setups it was meant to reject keep arriving, and the
    intent and the reality agree nowhere on the card. Same failure the AI
    label already guards against, one setting over.
    """
    risk = settings.risk
    parts = []
    if risk.regime_filter_enabled:
        mode = "hard" if risk.regime_hard_block else "monitor"
        parts.append(f"regime({risk.regime_symbol},{mode})")
    # Naming a disarmed gate by its threshold reads as "pauses at 0%", i.e. the
    # opposite of what it does. Say off when it is off.
    parts.append(
        f"drawdown≥{risk.drawdown_pause_pct:.0f}%(hard)"
        if risk.drawdown_pause_pct > 0 else "drawdown(off)"
    )
    ap_mode = "hard" if risk.autopause_hard_block else "monitor"
    parts.append(
        f"autopause<{risk.autopause_min_expectancy_r:+.2f}R/{risk.autopause_min_trades}({ap_mode})"
    )
    # Quoted with the minimum risk unit it implies, because that is the form
    # the number can be checked in: it reads straight against the 1R column
    # the diagnostic already prints for every strategy.
    max_cost_r = settings.max_cost_r
    floor = (settings.round_trip_cost_bps / 100.0) / max_cost_r if max_cost_r else 0.0
    parts.append(
        f"cost≤{max_cost_r:.2f}R(1R≥{floor:.2f}%)" if floor else "cost(off)"
    )
    return " · ".join(parts)


def _ai_mode_label(status: dict) -> str:
    """AI layer status as it actually is, not as it is configured.

    ``enabled`` is intent; ``available`` is whether a verdict can be produced.
    When they disagree every signal comes back ABSTAIN, which reads in the
    stats exactly like an AI that looked and had no opinion — so the startup
    card has to say which roles are dead, by name.
    """
    if not status.get("enabled"):
        return "OFF"
    if status.get("available"):
        return "MONITOR"
    reason = status.get("reason")
    if reason:
        return f"⚠️ ENABLED BUT UNAVAILABLE — {reason}"
    degraded = ", ".join(status.get("degraded_roles") or []) or "arbiter"
    return f"⚠️ ENABLED BUT UNAVAILABLE — no key/model for: {degraded}"


def _state_label(application) -> str:
    """Where history lives and whether it survives the next redeploy."""
    store = application.store
    outcomes = len(store.read("signal_outcomes", default=[]) or [])
    if state_is_persistent(application.settings.state_dir):
        return f"{store.base_dir} · {outcomes} outcomes (volume)"
    return (
        f"⚠️ {store.base_dir} · {outcomes} outcomes — EPHEMERAL, "
        "wiped on every redeploy. Attach a volume."
    )


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    log.info("Starting Wolf Crypto Tracker")

    application = build_application(settings)
    api = create_app(application)

    # Seed learning from a quick backtest so the bot doesn't start blind.
    application.warm_start_learning()

    scheduler = build_scheduler(application)
    scheduler.start()

    # Interactive Telegram commands (/analyze, /stats, /paper, /learning, ...).
    poller = TelegramPoller(application)
    poller.start()
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
    # Config-level check first: it costs nothing and names the exact env var,
    # where the probe below can only relay whatever the provider replied.
    for role_name in ("bull", "bear", "arbiter"):
        problem = getattr(settings.ai, role_name).mismatch()
        if problem:
            log.warning(
                "DEBATE_%s_PROVIDER/_MODEL disagree: %s",
                role_name.upper(), problem,
            )

    ai = ai_status(application, probe=True)
    if ai["enabled"] and ai["available"] and ai["degraded_roles"]:
        # The arbiter answers, so nothing downstream reports a fault — but a
        # debate missing a side is not the debate the verdicts are read as
        # having come from, and this is the only line that will ever say so.
        log.warning(
            "AI debate is answering, but these roles contribute nothing: %s. "
            "The arbiter is deciding on a one-sided argument.%s",
            ", ".join(ai["degraded_roles"]),
            "".join(
                f" [{role}: {why}]"
                for role, why in (ai.get("silent_reasons") or {}).items()
            ),
        )
    if ai["enabled"] and not ai["available"]:
        log.warning(
            "AI debate is ENABLED but cannot produce a verdict: %s. Every signal "
            "will come back ABSTAIN, which the statistics cannot tell apart from "
            "an AI with no opinion. Fix the provider key/balance or set "
            "AI_DEBATE_ENABLED=false.",
            ai.get("reason") or ", ".join(ai.get("degraded_roles") or []) or "unknown",
        )

    application.notifier.notify_startup({
        "sources": application.client.source_names,
        "detectors": application.screener.detector_names,
        "universe": application.screener.universe_size,
        "scan_min": settings.screener_interval_min,
        "track_min": settings.tracker_interval_min,
        "ai": settings.ai.enabled,
        # Reality, not intent: an enabled layer whose arbiter has no usable
        # client abstains on every signal while the card still reads MONITOR.
        # That is how a run of 29/29 ABSTAIN went unnoticed.
        "ai_mode": _ai_mode_label(ai),
        "risk_gates": _risk_gates_label(settings),
        "state": _state_label(application),
        # What has no topic of its own and therefore lands here. A recurring
        # digest falling back to the main channel is how the main channel
        # becomes that digest's feed.
        "unrouted": ", ".join(settings.telegram.unrouted_destinations()),
    })

    # Run an initial tracking pass so restarts resolve overdue signals promptly.
    try:
        application.tracker.check_pending()
    except Exception:
        log.exception("Initial tracking pass failed")

    try:
        uvicorn.run(api, host=settings.api_host, port=settings.api_port, log_level=settings.log_level.lower())
    finally:
        poller.stop()
        scheduler.shutdown(wait=False)
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
