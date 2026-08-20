"""Scheduler must add jobs in a runnable (non-paused) state, and only the jobs
whose components are actually wired."""

from __future__ import annotations

from types import SimpleNamespace

from wolf.scheduler import _valuation_universe, build_scheduler


class _Settings(SimpleNamespace):
    pass


def _onchain(**overrides) -> SimpleNamespace:
    defaults = dict(
        valuation_interval_min=60, whale_interval_min=10,
        premium_interval_min=10, macro_interval_min=60,
        valuation_max_symbols=15,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _app(*, notifier_enabled: bool = False, **overrides):
    notifier = SimpleNamespace(
        enabled=notifier_enabled,
        notify_flow=lambda text: None,
    )
    settings = _Settings(
        tracker_interval_min=5, screener_interval_min=10, stats_report_hours=24,
        news=SimpleNamespace(interval_min=30),
        reports=SimpleNamespace(
            majors_interval_min=60, radar_interval_min=30,
            pulse_interval_min=30, whale_interval_min=5,
        ),
        flow=SimpleNamespace(interval_min=30),
        onchain=_onchain(),
    )
    app = SimpleNamespace(
        settings=settings, notifier=notifier,
        tracker=SimpleNamespace(check_pending=lambda: None, stats=lambda: {}),
        screener=SimpleNamespace(run_cycle=lambda: None,
                                 current_universe=lambda: ["BTCUSDT", "ETHUSDT"]),
        news=None, news_synth=None, majors=None, radar=None, pulse=None,
        whale=None, flow=None,
        valuation_collector=None, whale_collector=None,
        premium_collector=None, macro_collector=None,
    )
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def _job_ids(app) -> set[str]:
    return {j.id for j in build_scheduler(app).get_jobs()}


# ── core jobs ─────────────────────────────────────────────────────────────
def test_core_jobs_have_a_next_run_time():
    sched = build_scheduler(_app())
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "track" in jobs and "scan" in jobs
    # next_run_time=None would mean PAUSED (never fires) — the bug we fixed.
    assert jobs["track"].next_run_time is not None
    assert jobs["scan"].next_run_time is not None


# ── collector jobs ────────────────────────────────────────────────────────
def test_no_collector_jobs_when_nothing_is_wired():
    ids = _job_ids(_app())
    assert not ids & {"onchain_collect", "whale_hl_collect",
                      "coinbase_premium_collect", "flow_macro_collect"}


def test_each_collector_adds_its_own_job():
    app = _app(
        valuation_collector=SimpleNamespace(collect=lambda symbols: {}),
        whale_collector=SimpleNamespace(scan=lambda: {}),
        premium_collector=SimpleNamespace(collect=lambda: {}),
        macro_collector=SimpleNamespace(collect=lambda: {}),
    )
    assert {"onchain_collect", "whale_hl_collect",
            "coinbase_premium_collect", "flow_macro_collect"} <= _job_ids(app)


def test_collectors_run_even_with_telegram_off():
    """A collector feeds the signal gates, not just a message — it must not be
    silenced by the notifier being disabled."""
    app = _app(notifier_enabled=False, whale_collector=SimpleNamespace(scan=lambda: {}))
    assert "whale_hl_collect" in _job_ids(app)


def test_collector_jobs_cannot_overlap_themselves():
    app = _app(whale_collector=SimpleNamespace(scan=lambda: {}))
    job = {j.id: j for j in build_scheduler(app).get_jobs()}["whale_hl_collect"]
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.next_run_time is not None


def test_collector_intervals_come_from_settings():
    app = _app(whale_collector=SimpleNamespace(scan=lambda: {}))
    app.settings.onchain = _onchain(whale_interval_min=7)
    job = {j.id: j for j in build_scheduler(app).get_jobs()}["whale_hl_collect"]
    assert job.trigger.interval.total_seconds() == 7 * 60


# ── flow report job ───────────────────────────────────────────────────────
def test_flow_report_job_added_when_reporter_and_notifier_are_live():
    app = _app(notifier_enabled=True, flow=SimpleNamespace(build=lambda: "text"))
    assert "flow_report" in _job_ids(app)


def test_flow_report_job_skipped_without_a_reporter():
    assert "flow_report" not in _job_ids(_app(notifier_enabled=True))


# ── valuation universe cap ────────────────────────────────────────────────
def test_valuation_universe_is_capped():
    """CoinGecko's key-less API cannot carry an unbounded dynamic universe."""
    app = _app()
    app.screener = SimpleNamespace(current_universe=lambda: [f"C{i}USDT" for i in range(50)])
    app.settings.onchain = _onchain(valuation_max_symbols=15)

    symbols = _valuation_universe(app)

    assert len(symbols) == 15
    assert symbols[0] == "C0USDT", "the cap keeps the head, where the majors are"


def test_valuation_universe_survives_a_universe_failure():
    app = _app()

    def _boom():
        raise RuntimeError("exchange down")

    app.screener = SimpleNamespace(current_universe=_boom)
    assert _valuation_universe(app) == []


# ── whale alerts ──────────────────────────────────────────────────────────
def _whale_doc() -> dict:
    return {"ts": "2026-08-20T04:00:00+00:00", "coins": {
        "SOL": {"direction": "LONG", "wallet_count": 4, "notional_usd": 2_400_000,
                "wallets": [{"addr": "0xabc123456789", "notional": 900_000, "is_new": True}]},
    }}


def _whale_app(*, doc=None, notifier_enabled=True, alert_enabled=True):
    app = _app(notifier_enabled=notifier_enabled,
               whale_collector=SimpleNamespace(scan=lambda: doc if doc is not None else _whale_doc()))
    app.settings.onchain = _onchain(whale_alert_enabled=alert_enabled)
    app.settings.timezone = "UTC"
    app.sent = []
    app.notifier.notify_whale = app.sent.append
    return app


def _run_whale_job(app) -> None:
    {j.id: j for j in build_scheduler(app).get_jobs()}["whale_hl_collect"].func()


def test_whale_job_alerts_the_whale_room_on_coordination():
    app = _whale_app()
    _run_whale_job(app)

    assert len(app.sent) == 1
    assert "WHALE COORDINATION" in app.sent[0] and "$SOL" in app.sent[0]


def test_whale_job_stays_silent_without_coordination():
    app = _whale_app(doc={"ts": "2026-08-20T04:00:00+00:00", "coins": {}})
    _run_whale_job(app)
    assert app.sent == []


def test_whale_alert_can_be_disabled_without_stopping_the_scan():
    """The snapshot feeds the signal gate; only the message is optional."""
    scanned = []
    app = _whale_app(alert_enabled=False)
    app.whale_collector = SimpleNamespace(scan=lambda: (scanned.append(1), _whale_doc())[1])
    _run_whale_job(app)

    assert scanned == [1], "the scan still ran"
    assert app.sent == []


def test_whale_scan_runs_even_when_telegram_is_off():
    scanned = []
    app = _whale_app(notifier_enabled=False)
    app.whale_collector = SimpleNamespace(scan=lambda: (scanned.append(1), _whale_doc())[1])
    _run_whale_job(app)

    assert scanned == [1]
    assert app.sent == []
