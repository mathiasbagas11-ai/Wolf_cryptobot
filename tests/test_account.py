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


# ── Net of costs ───────────────────────────────────────────────────────────
# The gross balance books pnl_pct and nothing pays a fee; the net balance beside
# it charges each trade its round trip on its own stop distance.

def _net_signal(status, pnl_pct, sl=98.0):
    sig = _signal(status, pnl_pct, sl=sl)
    sig.resolved_at = "2026-10-23T10:00:00+00:00"
    return sig


def test_the_net_balance_charges_each_trade_its_cost_on_its_own_stop(store):
    # 2% stop, 20bps round trip => 0.1R. A +1R win: gross +10, net +9.
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0, cost_bps=20.0)
    acc.apply(_net_signal(Status.TP_HIT.value, 2.0))
    p = acc.summary()
    assert p["balance"] == 1010.0
    assert p["net_balance"] == 1009.0
    assert p["gross_return_since_pct"] == 1.0
    assert p["net_return_pct"] == 0.9


def test_a_flat_timeout_leaves_the_gross_balance_alone_but_still_pays_its_fees(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0, cost_bps=20.0)
    assert acc.apply(_net_signal(Status.EXPIRED_FLAT.value, 0.0)) is None
    p = acc.summary()
    assert p["balance"] == 1000.0
    assert p["net_balance"] == 999.0
    assert p["net_trades"] == 1


def test_an_invalidated_signal_was_never_filled_and_pays_nothing(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0, cost_bps=20.0)
    acc.apply(_net_signal(Status.INVALIDATED.value, 0.0))
    assert "net_balance" not in acc.summary()


def test_the_net_curve_anchors_on_the_balance_its_first_trade_was_taken_against(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0, cost_bps=20.0)
    acc.apply(_net_signal(Status.SL_HIT.value, -2.0))
    p = acc.summary()
    # Anchored at 1000 (before the loss), not at 990 after it.
    assert p["gross_return_since_pct"] == -1.0
    assert p["net_balance"] == 989.0
    assert p["net_since"] == "2026-10-23"


def test_with_no_cost_configured_the_net_balance_is_the_gross_balance(store):
    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0)
    for pnl in (2.0, -2.0, 4.0):
        acc.apply(_net_signal(Status.TP_HIT.value if pnl > 0 else Status.SL_HIT.value, pnl))
    p = acc.summary()
    assert p["net_balance"] == p["balance"]


def test_paper_labels_the_balance_gross_and_prints_both_net_lines(store):
    from types import SimpleNamespace

    from wolf.config import Settings
    from wolf.notify.commands import CommandRouter

    acc = PaperAccount(store, start_balance=1000.0, risk_pct=1.0, cost_bps=20.0)
    sig = _net_signal(Status.TP_HIT.value, 2.0)
    sig.r_multiple = 1.0
    acc.apply(sig)
    app = SimpleNamespace(account=acc, settings=Settings(),
                          tracker=SimpleNamespace(outcomes=lambda: [sig]))
    reply = CommandRouter(app).handle("/paper")
    assert "gross, no fees charged" in reply
    assert "Net of 20bps" in reply and "1,009.00" in reply
    assert "Ledger 2026-10-23" in reply and "net +0.9%" in reply
