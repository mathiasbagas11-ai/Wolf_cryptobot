"""Tests for the paper trading account (Trade Report balance/PnL)."""

from __future__ import annotations

import pytest

from wolf.account import PaperAccount
from wolf.models import Signal, Status
from wolf.state import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path))


def _signal(status, pnl_pct, entry=100.0, sl=95.0):
    return Signal(
        symbol="BTCUSDT", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=110.0, sl=sl, strategy="MOMENTUM",
        status=status, pnl_pct=pnl_pct,
    )


def test_account_starts_at_configured_balance(store):
    acc = PaperAccount(store, start_balance=500.0, risk_pct=1.0)
    assert acc.balance == 500.0


def test_winning_trade_grows_balance_by_r_multiple(store):
    # entry 100, sl 95 -> 5% risk leg. PnL +10% => 2R. Risk 1% of 1000 = 10 => +20.
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0)
    snap = acc.apply(_signal(Status.TP_HIT.value, 10.0))
    assert snap["r_multiple"] == 2.0
    assert snap["pnl_amount"] == 20.0
    assert snap["balance"] == 1020.0
    assert acc.balance == 1020.0


def test_losing_trade_shrinks_balance(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0)
    snap = acc.apply(_signal(Status.SL_HIT.value, -5.0))  # -1R
    assert snap["r_multiple"] == -1.0
    assert snap["balance"] == 990.0


def test_risk_scale_shrinks_pnl_amount(store):
    # Bounce-guard half-size: same +10% (2R) trade risks half → half the P&L.
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0)
    sig = _signal(Status.TP_HIT.value, 10.0)
    sig.risk_scale = 0.5
    snap = acc.apply(sig)
    assert snap["r_multiple"] == 2.0        # R is per-unit, unchanged
    assert snap["pnl_amount"] == 10.0       # but the amount risked (and won) halved
    assert acc.balance == 1010.0


def test_invalidated_does_not_touch_balance(store):
    acc = PaperAccount(store, start_balance=1000.0)
    assert acc.apply(_signal(Status.INVALIDATED.value, 0.0)) is None
    assert acc.balance == 1000.0


# ── drawdown tracking ───────────────────────────────────────────────────────
def test_peak_tracks_high_water_mark(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0)
    acc.apply(_signal(Status.TP_HIT.value, 10.0))   # +20 -> 1020 (new peak)
    assert acc.peak == 1020.0
    acc.apply(_signal(Status.SL_HIT.value, -5.0))   # -1R of 1020 -> ~989.8
    assert acc.balance < 1020.0
    assert acc.peak == 1020.0                        # peak holds through the dip


def test_drawdown_pct_zero_at_peak(store):
    acc = PaperAccount(store, start_balance=1000.0)
    assert acc.drawdown_pct() == 0.0


def test_drawdown_pct_after_loss(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=10.0)
    acc.apply(_signal(Status.TP_HIT.value, 10.0))   # +2R of 10% = +200 -> 1200 peak
    acc.apply(_signal(Status.SL_HIT.value, -5.0))   # -1R of 10% of 1200 = -120 -> 1080
    # drawdown from 1200 peak to 1080 = 10%
    assert round(acc.drawdown_pct(), 1) == 10.0


def test_peak_backfilled_for_legacy_state(store):
    # State persisted before drawdown tracking has no "peak" key.
    from wolf.account import ACCOUNT_KEY
    store.write(ACCOUNT_KEY, {"balance": 800.0, "trades": 5, "realized": -200.0})
    acc = PaperAccount(store, start_balance=1000.0)
    assert acc.peak == 800.0          # backfilled to current balance
    assert acc.drawdown_pct() == 0.0
