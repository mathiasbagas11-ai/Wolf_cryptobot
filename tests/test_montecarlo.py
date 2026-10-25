"""Tests for the day-block Monte Carlo of the ledger.

The reason it deals whole days rather than single trades is the reason most of
these tests exist: positions that stop out through one market move are one
event recorded many times, and a trade shuffle quietly spreads that event
across the month.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from wolf.config import Settings
from wolf.models import Signal, Status
from wolf.montecarlo import MIN_DAYS, render, simulate
from wolf.notify.commands import CommandRouter
from wolf.tracker import OUTCOMES_KEY

_START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _sig(r: float, day: int, n: int = 0, *, risk_pct: float = 2.0,
         strategy: str = "MOMENTUM", status: Status | None = None) -> Signal:
    entry = 100.0
    if status is None:
        status = Status.TP_HIT if r > 0 else Status.SL_HIT
    at = (_START + timedelta(days=day, minutes=n)).isoformat()
    return Signal(
        id=f"d{day}n{n}", symbol=f"T{n}", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=entry * 1.1, sl=entry * (1 - risk_pct / 100),
        strategy=strategy, status=status.value, pnl_pct=r * risk_pct,
        r_multiple=r, resolved_at=at, exit_time=at,
    )


def _calm_month_with_one_crash(days: int = 20) -> list[Signal]:
    """Mixed small results every day, and one day where twelve stop out at once."""
    rows = []
    for d in range(days):
        if d == 10:
            rows += [_sig(-1.0, d, n) for n in range(12)]
        else:
            rows += [_sig(r, d, n) for n, r in enumerate((1.2, -1.0, 0.8, -0.6))]
    return rows


def test_a_day_of_clustered_stops_shows_a_deeper_drawdown_than_a_trade_shuffle():
    report = simulate(_calm_month_with_one_crash(), round_trip_bps=0, n_sims=2000)
    assert report["max_dd"]["p95"] > report["max_dd_trade_shuffle_p95"]


def test_costs_are_charged_on_each_trades_own_stop():
    rows = [_sig(1.0, d, risk_pct=1.0) for d in range(MIN_DAYS)]
    rows += [_sig(1.0, d, 1, risk_pct=4.0) for d in range(MIN_DAYS)]
    report = simulate(rows, round_trip_bps=20, n_sims=50)
    # 20bps over a 1% stop is 0.2R; over a 4% stop it is 0.05R.
    assert report["mean_gross_r"] == 1.0
    assert report["mean_net_r"] == round(1.0 - (0.2 + 0.05) / 2, 3)


def test_a_ledger_too_short_to_resample_says_so_instead_of_printing_numbers():
    report = simulate([_sig(1.0, d) for d in range(MIN_DAYS - 1)], n_sims=50)
    assert "error" in report
    assert "trading day" in render(report)
    assert "max DD" not in render(report)


def test_the_strategy_filter_keeps_only_that_strategy():
    rows = [_sig(1.0, d, 0, strategy="MOMENTUM") for d in range(MIN_DAYS)]
    rows += [_sig(-1.0, d, 1, strategy="SCALP") for d in range(MIN_DAYS)]
    report = simulate(rows, strategy="momentum", round_trip_bps=0, n_sims=50)
    assert report["trades"] == MIN_DAYS
    assert report["mean_gross_r"] == 1.0


def test_invalidated_and_still_open_rows_never_move_the_balance():
    rows = [_sig(1.0, d) for d in range(MIN_DAYS)]
    rows.append(_sig(-1.0, 0, 1, status=Status.INVALIDATED))
    rows.append(_sig(-1.0, 0, 2, status=Status.ACTIVE))
    report = simulate(rows, round_trip_bps=0, n_sims=50)
    assert report["trades"] == MIN_DAYS


def test_the_lived_path_compounds_the_ledger_in_the_order_it_happened():
    rows = [_sig(1.0 if d % 2 == 0 else -1.0, d) for d in range(MIN_DAYS + 1)]
    report = simulate(rows, risk_pct=1.0, round_trip_bps=0, n_sims=50)
    expected = (1.01 ** 4 * 0.99 ** 4 - 1) * 100
    assert report["actual"]["return"] == round(expected, 1)
    assert report["actual"]["losing_streak"] == 1


def test_every_week_in_the_ledger_is_listed_not_only_the_good_ones():
    report = simulate(_calm_month_with_one_crash(), round_trip_bps=0, n_sims=50)
    weeks = [w["week"] for w in report["weeks"]]
    assert weeks == sorted(set(weeks))
    assert sum(w["n"] for w in report["weeks"]) == report["trades"]
    assert min(w["net"] for w in report["weeks"]) < 0


def test_the_command_replies_with_the_card_and_help_lists_it(store):
    store.write(OUTCOMES_KEY, [s.to_dict() for s in _calm_month_with_one_crash()])
    app = SimpleNamespace(analyze=None, store=store, settings=Settings(),
                          tracker=SimpleNamespace(
                              outcomes=lambda: [Signal.from_dict(d) for d in store.read(OUTCOMES_KEY)]))
    router = CommandRouter(app)
    reply = router.handle("/montecarlo MOMENTUM")
    assert reply.startswith("<pre>") and "MONTE-CARLO | MOMENTUM" in reply
    assert "max DD" in router.handle("/mc")
    assert "/montecarlo" in router.handle("/help")
