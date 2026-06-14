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


def test_invalidated_does_not_touch_balance(store):
    acc = PaperAccount(store, start_balance=1000.0)
    assert acc.apply(_signal(Status.INVALIDATED.value, 0.0)) is None
    assert acc.balance == 1000.0
