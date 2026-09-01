"""Tests for pricing a round trip from the spread each signal actually faced."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wolf.diagnose import diagnose, render_digest
from wolf.exchange.sources import BinanceSource
from wolf.models import Signal, Status
from wolf.tracker import OUTCOMES_KEY, Tracker


# ── parsing the book ────────────────────────────────────────────────────────


def _book(*rows) -> list[dict]:
    return [
        {"symbol": sym, "bidPrice": str(bid), "bidQty": "1", "askPrice": str(ask), "askQty": "1"}
        for sym, bid, ask in rows
    ]


def test_the_spread_is_read_in_basis_points_of_the_mid():
    """A 1-point spread on a 1000-point mid is 10bps."""
    out = BinanceSource.parse_book_spread(_book(("BTCUSDT", 999.5, 1000.5)))
    assert out["BTCUSDT"] == pytest.approx(10.0)


def test_a_tight_book_and_a_wide_one_are_told_apart():
    """The whole reason for measuring: one constant cannot cover both."""
    out = BinanceSource.parse_book_spread(
        _book(("BTCUSDT", 99999.5, 100000.5), ("ALTUSDT", 0.995, 1.005))
    )
    assert out["BTCUSDT"] < 0.2      # sub-basis-point
    assert out["ALTUSDT"] > 90       # two orders of magnitude wider
    assert out["ALTUSDT"] / out["BTCUSDT"] > 100


def test_a_crossed_or_locked_book_is_dropped_not_reported_as_free():
    """A snapshot caught mid-update must not read as a costless round trip.

    Zero would be indistinguishable from a real, very tight market, and
    negative would credit the trade with a rebate it never earned.
    """
    out = BinanceSource.parse_book_spread(
        _book(("LOCKED", 100.0, 100.0), ("CROSSED", 100.5, 100.0), ("OK", 99.9, 100.1))
    )
    assert "LOCKED" not in out
    assert "CROSSED" not in out
    assert "OK" in out


def test_a_malformed_or_missing_payload_yields_nothing():
    assert BinanceSource.parse_book_spread(None) == {}
    assert BinanceSource.parse_book_spread({"error": "rate limited"}) == {}
    assert BinanceSource.parse_book_spread([{"symbol": "X"}]) == {}


# ── pricing the sample ──────────────────────────────────────────────────────


def _outcome(rows, *, r, spread_bps, risk_pct=1.0, n=0, status=Status.TP_HIT.value):
    entry = 100.0
    start = datetime.now(timezone.utc) - timedelta(hours=6)
    rows.append(Signal(
        symbol=f"SYM{n}", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=entry * 1.05, sl=entry * (1 - risk_pct / 100),
        strategy="SCALP", status=status, pnl_pct=r * risk_pct, r_multiple=r,
        spread_bps=spread_bps,
        activated_at=start.isoformat(),
        exit_time=(start + timedelta(hours=1)).isoformat(),
        resolved_at=(start + timedelta(hours=1)).isoformat(),
    ).to_dict())


def _diag(store, fake_client, tracker_settings, rows, **kw):
    store.write(OUTCOMES_KEY, rows)
    return diagnose(Tracker(store, fake_client, tracker_settings), **kw)


def test_the_round_trip_is_two_fees_plus_one_spread(store, fake_client, tracker_settings):
    """A taker buys the ask and sells the bid, so the spread is paid once."""
    rows = []
    for i in range(4):
        _outcome(rows, r=1.0, spread_bps=4.0, risk_pct=1.0, n=i)
    m = _diag(store, fake_client, tracker_settings, rows,
              taker_fee_bps=5.0)["cost"]["measured"]

    assert m["median_spread_bps"] == 4.0
    assert m["median_round_trip_bps"] == 14.0        # 2x5 + 4
    assert m["cost_r"] == pytest.approx(0.14)        # 14bps over a 1.00% risk unit


def test_the_measurement_sits_beside_the_assumption_not_on_top_of_it(
    store, fake_client, tracker_settings
):
    """The constant-based figure must not move when a measurement arrives.

    The sample straddles the change, and silently re-pricing it would break
    comparability with every digest already sent — the era break this report
    has had to drop a sample over once already.
    """
    rows = []
    for i in range(4):
        _outcome(rows, r=1.0, spread_bps=4.0, n=i)
    cost = _diag(store, fake_client, tracker_settings, rows,
                 round_trip_bps=20.0, taker_fee_bps=5.0)["cost"]

    assert cost["cost_r"] == 0.2                     # unchanged: 20bps / 1.00%
    assert cost["measured"]["cost_r"] == pytest.approx(0.14)


def test_only_the_signals_carrying_a_spread_are_priced(
    store, fake_client, tracker_settings
):
    """Coverage travels with the number, so a thin measurement reads as thin."""
    rows = []
    for i in range(3):
        _outcome(rows, r=1.0, spread_bps=4.0, n=i)
    for i in range(7):
        _outcome(rows, r=1.0, spread_bps=None, n=100 + i)
    m = _diag(store, fake_client, tracker_settings, rows)["cost"]["measured"]

    assert m["covered"] == 3
    assert m["sample"] == 10


def test_the_range_is_reported_not_just_the_middle(store, fake_client, tracker_settings):
    """A median alone would hide the dispersion the measurement exists to show."""
    rows = []
    for i, spread in enumerate((0.5, 3.0, 40.0)):
        _outcome(rows, r=1.0, spread_bps=spread, n=i)
    m = _diag(store, fake_client, tracker_settings, rows)["cost"]["measured"]

    assert m["min_spread_bps"] == 0.5
    assert m["max_spread_bps"] == 40.0
    assert m["median_spread_bps"] == 3.0


def test_cost_is_averaged_per_trade_because_expectancy_is_a_mean(
    store, fake_client, tracker_settings
):
    """Two trades at the same spread but different risk units cost differently.

    A 14bps round trip is 0.14R against a 1% stop and 0.014R against a 10%
    one. Taking the median risk first, as the constant-based figure does,
    would price both at the middle and misstate what netR subtracts.
    """
    rows = []
    _outcome(rows, r=1.0, spread_bps=4.0, risk_pct=1.0, n=0)
    _outcome(rows, r=1.0, spread_bps=4.0, risk_pct=10.0, n=1)
    m = _diag(store, fake_client, tracker_settings, rows,
              taker_fee_bps=5.0)["cost"]["measured"]

    assert m["cost_r"] == pytest.approx((0.14 + 0.014) / 2, abs=1e-3)


# ── reporting ───────────────────────────────────────────────────────────────


def test_the_digest_prints_the_measured_cost_beside_the_assumed_one(
    store, fake_client, tracker_settings
):
    rows = []
    for i, spread in enumerate((0.5, 4.0, 30.0)):
        _outcome(rows, r=1.0, spread_bps=spread, n=i)
    digest = render_digest(
        _diag(store, fake_client, tracker_settings, rows, taker_fee_bps=5.0)
    )
    assert "spread" in digest
    assert "0.50..30.00" in digest
    assert "round trip" in digest
    assert "assumed" in digest


def test_an_uncovered_window_says_so_rather_than_going_quiet(
    store, fake_client, tracker_settings
):
    """Silence would read as "measured, no difference" — the opposite of true."""
    rows = []
    for i in range(3):
        _outcome(rows, r=1.0, spread_bps=None, n=i)
    digest = render_digest(_diag(store, fake_client, tracker_settings, rows))
    assert "no signal in this window recorded a spread" in digest
    assert "0/3" in digest
