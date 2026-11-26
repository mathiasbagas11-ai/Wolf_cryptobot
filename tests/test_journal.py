"""Tests for the journal payload behind the dashboard."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from wolf.api import create_app
from wolf.app import build_application
from wolf.journal import journal
from wolf.models import Signal, Status
from wolf.tracker import OUTCOMES_KEY


def _sig(r, at, strategy="MOMENTUM", risk_pct=2.0):
    return Signal(
        symbol="T", signal_type="SCREENER", direction="LONG", entry_price=100.0,
        tp=110.0, sl=100 - risk_pct, strategy=strategy,
        status=(Status.TP_HIT if r > 0 else Status.SL_HIT).value,
        pnl_pct=r * risk_pct, r_multiple=r, resolved_at=at, exit_time=at,
    )


def test_the_curve_compounds_net_of_each_trades_own_cost():
    rows = [_sig(1.0, "2026-10-20T01:00:00+00:00"), _sig(-1.0, "2026-10-20T02:00:00+00:00")]
    j = journal(rows, risk_pct=1.0, round_trip_bps=20)
    # 2% stop, 20bps => 0.1R each: +0.9R then -1.1R at 1%.
    assert j["curve"][-1]["net"] == round((1.009 * 0.989 - 1) * 100, 2)
    assert j["curve"][-1]["gross"] == round((1.01 * 0.99 - 1) * 100, 2)


def test_calendar_days_are_the_owners_days_not_utc():
    j = journal([_sig(1.0, "2026-10-20T20:00:00+00:00")], tz="Asia/Jakarta")
    assert j["days"][0]["date"] == "2026-10-21"


def test_headline_numbers_include_profit_factor_and_the_breakeven_bar():
    rows = [_sig(2.0, f"2026-10-2{i}T01:00:00+00:00") for i in range(2)]
    rows += [_sig(-1.0, f"2026-10-2{i}T02:00:00+00:00") for i in range(3)]
    o = journal(rows, round_trip_bps=0)["overall"]
    assert o["win_rate"] == 40.0
    assert o["profit_factor"] == round(4.0 / 3.0, 2)
    assert o["breakeven_wr"] == round(1 / 3 * 100, 1)


def test_setups_are_listed_by_name_with_no_winner_picked():
    rows = [_sig(1.0, "2026-10-20T01:00:00+00:00", "TRAP"),
            _sig(-1.0, "2026-10-20T02:00:00+00:00", "MOMENTUM")]
    assert list(journal(rows)["by_strategy"]) == ["MOMENTUM", "TRAP"]


def test_the_journal_endpoint_is_behind_the_key(settings):
    app_obj = build_application(replace(settings, api_key="k"))
    app_obj.store.write(OUTCOMES_KEY, [_sig(1.0, "2026-10-20T01:00:00+00:00").to_dict()])
    api = TestClient(create_app(app_obj))
    assert api.get("/journal").status_code == 401
    body = api.get("/journal", headers={"X-API-Key": "k"}).json()
    assert body["overall"]["n"] == 1 and body["days"]
