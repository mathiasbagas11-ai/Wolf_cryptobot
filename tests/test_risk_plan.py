"""Tests for the trade-plan / position-sizing engine and the /calc command."""

from __future__ import annotations

from types import SimpleNamespace

from wolf.config import RiskSettings
from wolf.notify.commands import CommandRouter
from wolf.risk_plan import build_plan


def test_fixed_fractional_sizing():
    # entry 100, sl 98 -> 2% stop. $1000 @ 2% risk -> risk $20, notional $1000.
    p = build_plan(100, 98, is_long=True, balance=1000, risk_pct=2.0, max_leverage=10)
    assert p.risk_amount == 20.0
    assert p.notional == 1000.0
    assert p.stop_dist_pct == 2.0


def test_leverage_capped_and_liquidation_safe():
    p = build_plan(100, 98, is_long=True, balance=1000, risk_pct=2.0, max_leverage=10)
    assert p.leverage == 10                  # capped at the configured max
    assert p.margin == 100.0                 # notional / leverage
    assert abs(p.liq_price - 90.5) < 1e-6    # 100 * (1 - (1/10 - 0.005))
    assert p.liq_safe                        # 9.5% liq dist > 2x the 2% stop


def test_wide_stop_lowers_leverage():
    # 20% stop -> safe leverage must drop well below the cap.
    p = build_plan(100, 80, is_long=True, balance=1000, risk_pct=2.0, max_leverage=10)
    assert p.leverage == 2
    assert p.liq_safe
    assert p.notional == 100.0  # 20 / 0.20


def test_extreme_stop_flagged_unsafe():
    # 70% stop can't be made liquidation-safe even at 1x -> flagged.
    p = build_plan(100, 30, is_long=True, balance=1000, risk_pct=2.0, max_leverage=10)
    assert p.leverage == 1
    assert not p.liq_safe


def test_short_liquidation_above_entry():
    p = build_plan(100, 102, is_long=False, balance=1000, risk_pct=2.0, max_leverage=10)
    assert p.liq_price > 100
    assert abs(p.liq_price - 109.5) < 1e-6


def test_build_plan_rejects_bad_inputs():
    assert build_plan(0, 98, True, 1000, 2.0) is None
    assert build_plan(100, 100, True, 1000, 2.0) is None  # zero stop distance
    assert build_plan(100, 98, True, 0, 2.0) is None


# ── /calc command ────────────────────────────────────────────────────────────
def _calc_app(candidate):
    analyze = SimpleNamespace(latest_setup=lambda sym: candidate)
    account = SimpleNamespace(balance=1000.0)
    settings = SimpleNamespace(risk=RiskSettings(), paper_risk_pct=2.0)
    return SimpleNamespace(analyze=analyze, account=account, settings=settings)


def test_calc_with_setup():
    cand = SimpleNamespace(entry_price=100, sl=98, tp=106, direction="LONG", strategy="SWING")
    out = CommandRouter(_calc_app(cand)).handle("/calc BTC 500")
    assert "TRADE PLAN" in out and "Leverage" in out
    assert "BTCUSDT LONG" in out


def test_calc_no_setup():
    out = CommandRouter(_calc_app(None)).handle("/calc ETH 500")
    assert "No active setup" in out


def test_calc_usage_without_args():
    out = CommandRouter(_calc_app(None)).handle("/calc")
    assert "Usage" in out
