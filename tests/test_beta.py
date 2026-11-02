"""Tests for /beta: how much of each trade's R was BTC moving under it."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from wolf.beta import X_CLIP, beta_report, clustered_ols, price_at, render
from wolf.models import Candle, Signal, Status
from wolf.notify.commands import CommandRouter

from tests.test_whatif import _router_app

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
T0 = int((NOW - timedelta(days=30)).timestamp() * 1000)


def _btc(path):
    """Hourly bars whose open/close follow ``path(hour_index)``."""
    return [Candle(time=T0 + i * 3_600_000, open=path(i), high=max(path(i), path(i + 1)) + 1,
                   low=min(path(i), path(i + 1)) - 1, close=path(i + 1), volume=1.0)
            for i in range(30 * 24)]


def _trade(start_h: int, hours: int, *, long=True, r=0.0, risk_pct=1.0, strategy="SCALP"):
    start = datetime.fromtimestamp(T0 / 1000, tz=timezone.utc) + timedelta(hours=start_h)
    end = start + timedelta(hours=hours)
    entry = 100.0
    sl = entry * (1 - risk_pct / 100) if long else entry * (1 + risk_pct / 100)
    return Signal(symbol="SOLUSDT", signal_type="PUMP", strategy=strategy, direction="LONG" if long else "SHORT",
                  entry_price=entry, tp=entry, sl=sl, created_at=start.isoformat(),
                  activated_at=start.isoformat(), status=Status.SL_HIT.value,
                  exit_time=end.isoformat(), resolved_at=end.isoformat(), r_multiple=r)


def test_price_is_interpolated_inside_the_bar_and_refused_outside_history():
    bars = _btc(lambda i: 100.0 + i)
    times = [b.time for b in bars]
    mid = datetime.fromtimestamp((T0 + 1_800_000) / 1000, tz=timezone.utc)
    assert price_at(bars, times, mid) == 100.5
    before = datetime.fromtimestamp((T0 - 1) / 1000, tz=timezone.utc)
    assert price_at(bars, times, before) is None


def test_a_ledger_that_is_only_btc_has_slope_one_and_no_rest(fake_client):
    """Every trade's R equals BTC's move toward it in its own R: the card must
    attribute the whole mean to BTC and leave nothing over."""
    path = lambda i: 100.0 * (1 + 0.004 * math.sin(i / 3.0))  # noqa: E731
    fake_client.klines["BTCUSDT"] = _btc(path)
    trades = []
    for k in range(40):
        start, hours, long = k * 13 % 600, 2 + k % 5, k % 3 != 0
        move = (path(start + hours) / path(start) - 1) * 100
        trades.append(_trade(start, hours, long=long, r=move if long else -move))
    report = beta_report(trades, fake_client, now=NOW)
    f = report["btc"]
    assert abs(f["b"] - 1.0) < 1e-6 and abs(f["a"]) < 1e-6 and f["r2"] > 0.999
    assert abs(f["b"] * f["mean_x"] - f["mean_y"]) < 1e-6


def test_the_move_is_signed_toward_the_trade(fake_client):
    """BTC rising is WITH a long and AGAINST a short."""
    fake_client.klines["BTCUSDT"] = _btc(lambda i: 100.0 + 0.1 * i)
    trades = [_trade(10 * k, 3, long=True, r=0.5) for k in range(5)]
    trades += [_trade(10 * k + 100, 3, long=False, r=-0.5) for k in range(5)]
    f = beta_report(trades, fake_client, now=NOW)["btc"]
    assert f["with_n"] == 5 and f["with_r"] == 0.5
    assert f["against_n"] == 5 and f["against_r"] == -0.5


def test_a_tiny_stop_under_a_big_move_is_clipped_not_left_to_set_the_slope(fake_client):
    fake_client.klines["BTCUSDT"] = _btc(lambda i: 100.0 + i)
    tiny = [_trade(24 * k, 10, risk_pct=0.1, r=1.0) for k in range(5)]
    wide = [_trade(24 * k + 12, 1, risk_pct=5.0, r=0.0) for k in range(5)]
    f = beta_report(tiny + wide, fake_client, now=NOW)["btc"]
    assert f["clipped"] == 5
    # Unclipped, the tiny stops sit near x=100R and the slope collapses to ~0.01;
    # clipped at X_CLIP they still say "rode BTC" at a sane scale.
    assert f["b"] > 1.0 / (X_CLIP + 1)


def test_trades_on_one_day_count_as_one_observation():
    """Clustering by day: the df is days less one, not trades less two."""
    xs = [0.1 * i for i in range(30)]
    ys = [x + (0.3 if i % 2 else -0.3) for i, x in enumerate(xs)]
    days = [f"d{i // 10}" for i in range(30)]
    fit = clustered_ols(ys, xs, days)
    assert fit["df"] == 2 and fit["days"] == 3
    assert abs(fit["t_b"]) < abs(fit["t_b_nom"]) or fit["se_b"] > 0


def test_trades_outside_the_btc_history_are_skipped_and_counted(fake_client):
    fake_client.klines["BTCUSDT"] = _btc(lambda i: 100.0 + i)
    inside = [_trade(24 * k, 3, r=0.2 * k) for k in range(5)]
    outside = replace(inside[0], activated_at=(NOW - timedelta(days=60)).isoformat(),
                      created_at=(NOW - timedelta(days=60)).isoformat())
    report = beta_report(inside + [outside], fake_client, now=NOW)
    assert report["unpriced"] == 1 and report["btc"]["n"] == 5


def test_every_group_row_carries_padj(fake_client):
    fake_client.klines["BTCUSDT"] = _btc(lambda i: 100.0 + 0.05 * i)
    trades = [_trade(17 * k, 4, long=k % 2 == 0, r=(-1) ** k * 0.3,
                     strategy="MOMENTUM" if k % 3 else "TRAP") for k in range(24)]
    report = beta_report(trades, fake_client, now=NOW)
    names = [g["name"] for g in report["groups"]]
    assert names[:2] == ["LONG", "SHORT"] and "MOMENTUM" in names and "TRAP" in names
    assert all("padj" in g for g in report["groups"])
    assert "never alpha" in render(report)


def test_no_btc_history_says_so_instead_of_guessing(fake_client):
    report = beta_report([_trade(1, 2)], fake_client, now=NOW)
    assert "no 1h history" in render(report)


def test_beta_is_reachable_as_a_telegram_command(store, fake_client):
    reply = CommandRouter(_router_app(store, fake_client)).handle("/beta")
    assert reply.startswith("<pre>BETA")
    assert "/beta" in CommandRouter(_router_app(store, fake_client)).handle("/help")
