"""Tests for re-booking outcomes whose banked rungs were never counted.

Three exit paths close a scaled position and only two priced it as scaled. The
timeout path valued the whole position at the price on the clock, as though the
slice sold at TP1 were still open — under-booking a trade that drifted back and
over-booking one parked above the rung, and in the dead-band case filing it as
having said nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.account import ACCOUNT_KEY, PaperAccount
from wolf.config import TrackerSettings
from wolf.learning.engine import MEMORY_KEY
from wolf.models import Signal, Status
from wolf.rebank import rebank_outcomes, replay_balance
from wolf.tracker import OUTCOMES_KEY

_LADDER = [
    {"level": 1, "price": 102.0, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 104.0, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 106.0, "allocation": 0.2, "r_multiple": 3.0},
]


def _outcome(*, status, exit_price, tps_hit, n=0, entry=100.0, sl=98.0, strategy="MOMENTUM"):
    """A row as the buggy timeout path would have written it: the whole
    position priced at the timeout price."""
    start = datetime.now(timezone.utc) - timedelta(hours=10) + timedelta(minutes=5 * n)
    pnl = (exit_price - entry) / entry * 100
    return Signal(
        id=f"s{n}", symbol=f"T{n}", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=106.0, sl=sl, strategy=strategy, tp_ladder=_LADDER,
        tps_hit=list(tps_hit), status=status.value, exit_price=exit_price,
        pnl_pct=round(pnl, 3), r_multiple=round(pnl / 2.0, 3),
        activated_at=start.isoformat(),
        exit_time=(start + timedelta(hours=1)).isoformat(),
        resolved_at=(start + timedelta(hours=1)).isoformat(),
    ).to_dict()


def test_a_timeout_that_banked_tp1_is_rebooked_upward(store):
    """Drifted back toward entry: the slice sold at TP1 was forgotten, so the
    trade was recorded at a fraction of what it made."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=100.5, tps_hit=[1])])
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["rebooked"] == 1
    row = store.read(OUTCOMES_KEY)[0]
    # 50% at +2.0% (TP1) + 50% at +0.5% = +1.25%
    assert row["pnl_pct"] == 1.25
    assert row["r_multiple"] == 0.625


def test_a_timeout_parked_above_the_rung_is_rebooked_downward(store):
    """The error runs both ways: nothing was credited as sold at TP1, so a
    trade parked above it was booked as if the whole position rode to the end."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=103.0, tps_hit=[1])])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    row = store.read(OUTCOMES_KEY)[0]
    assert row["pnl_pct"] == 2.5          # was 3.0
    assert row["r_multiple"] == 1.25


def test_a_flat_expiry_can_become_a_graded_win(store):
    """The worst case. Blended, the trade cleared the dead band — but it was
    filed as having said nothing, which keeps it out of the win rate and out of
    the paper balance entirely."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_FLAT,
                                        exit_price=100.2, tps_hit=[1])])
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["status_changed"] == 1
    row = store.read(OUTCOMES_KEY)[0]
    assert row["status"] == Status.EXPIRED_WIN.value
    assert row["pnl_pct"] == 1.1          # 50% at +2.0% + 50% at +0.2%
    assert Status(row["status"]).is_graded


def test_rows_with_no_banked_rung_are_left_alone(store):
    """Nothing was sold early, so the single-exit price is already correct."""
    before = [_outcome(status=Status.EXPIRED_LOSS, exit_price=99.0, tps_hit=[])]
    store.write(OUTCOMES_KEY, before)
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["rebooked"] == 0
    assert store.read(OUTCOMES_KEY) == before


def test_blended_paths_are_not_touched(store):
    """A TP_HIT row already carries a blended PnL and a synthetic exit price
    derived from it. Re-blending that price would book the trade twice."""
    row = _outcome(status=Status.TP_HIT, exit_price=101.0, tps_hit=[1])
    store.write(OUTCOMES_KEY, [row])
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["rebooked"] == 0
    assert store.read(OUTCOMES_KEY)[0]["pnl_pct"] == row["pnl_pct"]


def test_a_dry_run_writes_nothing(store):
    """A ledger is not something to rewrite before looking at what would move."""
    before = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1])]
    store.write(OUTCOMES_KEY, list(before))
    report = rebank_outcomes(store, TrackerSettings(), dry_run=True)

    assert report["rebooked"] == 1 and report["dry_run"] is True
    assert store.read(OUTCOMES_KEY) == before


def test_rebanking_twice_changes_nothing_the_second_time(store):
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=100.5, tps_hit=[1], n=i)
                               for i in range(3)])
    assert rebank_outcomes(store, TrackerSettings(), dry_run=False)["rebooked"] == 3
    assert rebank_outcomes(store, TrackerSettings(), dry_run=False)["rebooked"] == 0


def test_learning_memory_is_never_touched(store):
    """It lives under its own key and holds trades seeded from backtests that
    never appear in the outcome log — rebuilding it from that log would discard
    them. Its staleness is documented instead."""
    memory = {"strategies": {"MOMENTUM": {"trades": 40, "wins": 22,
                                          "pnl_sum": 15.5, "r_sum": 7.75}},
              "symbols": {}}
    store.write(MEMORY_KEY, memory)
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_FLAT,
                                        exit_price=100.2, tps_hit=[1])])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert store.read(MEMORY_KEY) == memory


def test_the_balance_is_replayed_rather_than_patched(store):
    """It compounds — each trade risks a share of the balance at that time — so
    a delta cannot be added to it after the fact."""
    rows = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1], n=i)
            for i in range(4)]
    store.write(OUTCOMES_KEY, rows)
    store.write(ACCOUNT_KEY, {"balance": 9999.0, "trades": 99,
                              "realized": 0.0, "peak": 9999.0})

    report = rebank_outcomes(store, TrackerSettings(), start_balance=1000.0,
                             risk_pct=1.0, dry_run=False)
    assert report["balance_before"] == 9999.0
    # Four re-booked trades at +0.625R, 1% of a compounding balance each.
    assert 1000.0 < report["balance_after"] < 1030.0
    assert PaperAccount(store, 1000.0, 1.0).balance == report["balance_after"]


def test_replaying_an_empty_log_returns_the_starting_balance(store):
    assert replay_balance(store, start_balance=1000.0) == 1000.0


def test_the_real_timeout_price_survives_the_rewrite(store):
    """``exit_price`` becomes a synthetic figure consistent with the blended
    PnL, which destroys the one input the blend reads. The market price is kept
    beside it, and that — not a marker — is what makes a second run a no-op."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=100.5, tps_hit=[1])])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    row = store.read(OUTCOMES_KEY)[0]
    assert row["timeout_price"] == 100.5
    assert row["exit_price"] != 100.5   # synthetic, consistent with +1.25%
    assert round(row["exit_price"], 6) == 101.25
