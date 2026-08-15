"""Tests for the signal lifecycle tracker — the core of the bot."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from wolf.config import LadderSettings, TrackerSettings
from wolf.models import Candle, Status
from wolf.tracker import Tracker, normalize_ladder

# A 1:3 ladder off entry 100 / stop 95: 1R = 5, rungs at 105 / 110 / 115,
# closing 50% / 30% / 20% of the position.
LADDER_1_3 = [
    {"level": 1, "price": 105, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 110, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 115, "allocation": 0.2, "r_multiple": 3.0},
]


def _candles_after(now_ms: int, ohlc: list[tuple[float, float, float, float]]):
    """Build 15m candles strictly after ``now_ms``."""
    step = 900_000
    return [
        Candle(time=now_ms + (i + 1) * step, open=o, high=h, low=l, close=c, volume=100.0)
        for i, (o, h, l, c) in enumerate(ohlc)
    ]


# ── normalize_ladder ────────────────────────────────────────────────────
def test_normalize_ladder_orders_long_nearest_first():
    rungs = normalize_ladder(
        [{"level": 2, "price": 110}, {"level": 1, "price": 105}], 110, 95, 100, is_long=True
    )
    assert [r.price for r in rungs] == [105, 110]
    assert [r.level for r in rungs] == [1, 2]


def test_normalize_ladder_drops_wrong_side_rungs():
    # For a LONG, a TP below entry is invalid and must be dropped.
    rungs = normalize_ladder([{"level": 1, "price": 95}], 105, 90, 100, is_long=True)
    assert [r.price for r in rungs] == [105]  # fell back to single tp


def test_normalize_ladder_fallback_single_tp():
    rungs = normalize_ladder(None, 110, 95, 100, is_long=True)
    assert len(rungs) == 1 and rungs[0].price == 110


# ── record_signal validation ─────────────────────────────────────────────
def test_record_rejects_wrong_side_long(tracker):
    assert tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 100, tp=95, sl=90) is None


def test_record_rejects_nonpositive(tracker):
    assert tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 0, tp=110, sl=95) is None


def test_record_accepts_valid_and_dedups(tracker):
    s1 = tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95)
    assert s1 is not None
    # Same symbol+direction within the dedup window -> rejected.
    s2 = tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95)
    assert s2 is None
    assert len(tracker.active_signals()) == 1


# ── lifecycle ─────────────────────────────────────────────────────────────
def test_long_signal_hits_tp(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    sig = tracker.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [
        (100, 102, 99, 101),
        (101, 106, 100, 105),   # TP1
        (105, 111, 104, 110),   # TP2 -> terminal
    ])
    resolved = tracker.check_pending()
    assert len(resolved) == 1
    assert resolved[0].status == Status.TP_HIT.value
    assert resolved[0].pnl_pct == 10.0


# ── TP1-banks-win grading (partial-win after breakeven stop) ─────────────────
def _tp1_then_breakeven(store, fake_client):
    """Record a LONG that hits TP1 (105) then pulls back to the breakeven stop."""
    tracker_stub = Tracker(store, fake_client, TrackerSettings())
    sig = tracker_stub.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [
        (100, 102, 99, 101),
        (101, 106, 100, 105),   # TP1 at 105 -> stop trails to breakeven (100)
        (100, 101, 99, 100),    # low pierces the breakeven stop
    ])


def test_tp1_then_breakeven_stop_is_loss_by_default(store, fake_client):
    # Legacy all-or-nothing rule: TP1 banked then stopped at BE == a loss.
    _tp1_then_breakeven(store, fake_client)
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=False))
    resolved = tracker.check_pending()
    assert resolved[0].status == Status.SL_HIT.value
    assert resolved[0].pnl_pct == 0.0


def test_partial_win_weights_the_ladder_by_allocation(store, fake_client):
    """A front-loaded ladder banks what it actually closed at TP1, not a third.

    With 50% off at TP1 (+5%) and the remaining 50% stopped at breakeven, the
    trade returns +2.5% — an even three-way split would have booked only the
    first of three slices and understated it.
    """
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=115, sl=95,
        entry_mode="MOMENTUM_NOW", tps=LADDER_1_3,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [
        (100, 106, 99, 105),   # TP1 fills, stop trails to breakeven
        (105, 106, 99, 100),   # back to entry -> breakeven stop
    ])
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    r = tracker.check_pending()[0]
    assert r.status == Status.TP_HIT.value
    assert r.pnl_pct == 2.5          # 0.5 x (+5%) + 0.5 x 0%
    assert r.r_multiple == 0.5       # half a position banked at 1R


def test_one_bar_sweeping_tp1_and_the_stop_keeps_the_tp1_profit(store, fake_client):
    """A bar reaching TP1 *and* the stop is graded TP1-first by default.

    It closes down, so its high printed before its low: TP1 filled, the stop
    moved to breakeven, and the sell-off closed the rest there. Assuming the
    stop went first would write off a TP1 that plainly filled.
    """
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=115, sl=95,
        entry_mode="MOMENTUM_NOW", tps=LADDER_1_3,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 106, 94, 96)])
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    r = tracker.check_pending()[0]
    assert r.status == Status.TP_HIT.value
    assert r.pnl_pct == 2.5
    assert r.tps_hit == [1]


def test_intrabar_sequence_follows_the_bar_direction(store, fake_client):
    """The mirror bar — closing up — is read low-first, so the stop wins.

    Without this the rule would be "always optimistic" rather than an
    inference, and every genuine stop-out that later recovered inside the same
    bar would be graded a partial win.
    """
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=115, sl=95,
        entry_mode="MOMENTUM_NOW", tps=LADDER_1_3,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 106, 94, 105)])
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    r = tracker.check_pending()[0]
    assert r.status == Status.SL_HIT.value
    assert r.r_multiple == -1.0


def test_tp_fill_bar_does_not_trigger_its_own_breakeven_stop(store, fake_client):
    """A fresh long routinely trades back to entry before it runs.

    The breakeven stop only exists *after* TP1 fills, so the bar that filled it
    must not be closed out by the stop it just created.
    """
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=115, sl=95,
        entry_mode="MOMENTUM_NOW", tps=LADDER_1_3,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(101, 106, 100, 105)])
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    assert tracker.check_pending() == []          # still running
    assert tracker.active_signals()[0].tps_hit == [1]


def test_pessimistic_mode_assumes_the_stop_went_first(store, fake_client):
    """INTRABAR_TP_FIRST=0 restores the strictly conservative reading."""
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=115, sl=95,
        entry_mode="MOMENTUM_NOW", tps=LADDER_1_3,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 106, 94, 96)])
    tracker = Tracker(
        store, fake_client, TrackerSettings(tp1_banks_win=True),
        ladder=LadderSettings(intrabar_tp_first=False),
    )
    r = tracker.check_pending()[0]
    assert r.status == Status.SL_HIT.value
    assert r.r_multiple == -1.0


def test_intrabar_sequence_mirrors_correctly_for_shorts(store, fake_client):
    """For a short the profit side is the low, so the bar reading flips."""
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "SOLUSDT", "SCREENER", "SHORT", 100, tp=85, sl=105,
        entry_mode="MOMENTUM_NOW",
        tps=[
            {"level": 1, "price": 95, "allocation": 0.5},
            {"level": 2, "price": 90, "allocation": 0.3},
            {"level": 3, "price": 85, "allocation": 0.2},
        ],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    # Closes up -> low printed first, and for a short the TPs sit at the low.
    fake_client.klines["SOLUSDT"] = _candles_after(now_ms, [(100, 106, 94, 104)])
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    r = tracker.check_pending()[0]
    assert r.status == Status.TP_HIT.value
    assert r.tps_hit == [1]
    assert r.pnl_pct == 2.5


def test_legacy_ladder_without_allocations_still_splits_evenly(store, fake_client):
    """Old rungs must not be re-graded under the new front-loaded weights."""
    rungs = normalize_ladder(
        [{"level": 1, "price": 105}, {"level": 2, "price": 110}], 110, 95, 100, is_long=True
    )
    assert [r.allocation for r in rungs] == [0.5, 0.5]
    assert sum(r.allocation for r in rungs) == 1.0


def test_normalize_ladder_renormalises_after_dropping_a_rung(store, fake_client):
    """A dropped rung must not leave part of the position unaccounted for."""
    rungs = normalize_ladder(
        [
            {"level": 1, "price": 90, "allocation": 0.5},   # wrong side, dropped
            {"level": 2, "price": 110, "allocation": 0.3},
            {"level": 3, "price": 115, "allocation": 0.2},
        ],
        115, 95, 100, is_long=True,
    )
    assert [r.price for r in rungs] == [110, 115]
    assert sum(r.allocation for r in rungs) == 1.0


def test_tp1_then_breakeven_stop_banks_partial_win(store, fake_client):
    # With the fix: half booked at TP1 (+5%), half at BE (0%) -> +2.5% win.
    _tp1_then_breakeven(store, fake_client)
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    resolved = tracker.check_pending()
    r = resolved[0]
    assert r.status == Status.TP_HIT.value
    assert Status(r.status).is_win
    assert r.pnl_pct == 2.5
    # Exit price must be consistent with the booked PnL (no contradictory report).
    assert r.exit_price == 100 * (1 + 2.5 / 100)  # 102.5


def test_tp1_then_breakeven_stop_partial_win_short(store, fake_client):
    # Short mirror: TP1 at 95 (+5%), stop trails to BE (100) -> +2.5% win.
    stub = Tracker(store, fake_client, TrackerSettings())
    sig = stub.record_signal(
        "SOLUSDT", "SCREENER", "SHORT", 100, tp=90, sl=105,
        entry_mode="MOMENTUM_NOW", tps=[{"level": 1, "price": 95}, {"level": 2, "price": 90}],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["SOLUSDT"] = _candles_after(now_ms, [
        (100, 101, 94, 95),     # TP1 at 95 -> stop trails to breakeven (100)
        (100, 101, 99, 100),    # high pierces the breakeven stop
    ])
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    r = tracker.check_pending()[0]
    assert r.status == Status.TP_HIT.value
    assert r.pnl_pct == 2.5
    assert r.exit_price == 100 * (1 - 2.5 / 100)  # 97.5


def test_stop_before_tp1_still_loss_when_enabled(store, fake_client):
    # No rung banked -> a real loss even with the flag on.
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    sig = tracker.record_signal(
        "ETHUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["ETHUSDT"] = _candles_after(now_ms, [(100, 101, 94, 96)])
    resolved = tracker.check_pending()
    assert resolved[0].status == Status.SL_HIT.value
    assert resolved[0].pnl_pct == -5.0


def test_full_ladder_win_unaffected_when_enabled(store, fake_client):
    # Reaching the final rung stays a full win at the final-rung PnL.
    tracker = Tracker(store, fake_client, TrackerSettings(tp1_banks_win=True))
    sig = tracker.record_signal(
        "SOLUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["SOLUSDT"] = _candles_after(now_ms, [
        (100, 106, 100, 105),   # TP1
        (105, 111, 104, 110),   # TP2 -> terminal
    ])
    resolved = tracker.check_pending()
    assert resolved[0].status == Status.TP_HIT.value
    assert resolved[0].pnl_pct == 10.0


def test_long_signal_hits_sl(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    sig = tracker.record_signal(
        "ETHUSDT", "SCREENER", "LONG", 100, tp=110, sl=95, entry_mode="MOMENTUM_NOW"
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["ETHUSDT"] = _candles_after(now_ms, [
        (100, 101, 94, 96),  # low pierces SL at 95
    ])
    resolved = tracker.check_pending()
    assert resolved[0].status == Status.SL_HIT.value
    assert resolved[0].pnl_pct == -5.0


def test_short_signal_hits_tp(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    sig = tracker.record_signal(
        "SOLUSDT", "SCREENER", "SHORT", 100, tp=90, sl=105, entry_mode="MOMENTUM_NOW"
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["SOLUSDT"] = _candles_after(now_ms, [
        (100, 101, 89, 90),  # low reaches TP at 90
    ])
    resolved = tracker.check_pending()
    assert resolved[0].status == Status.TP_HIT.value
    assert resolved[0].pnl_pct == 10.0


def test_retest_never_touched_invalidates(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    # RETEST_WAIT entry at 90 for a LONG, but price stays above -> never active.
    tracker.record_signal(
        "ADAUSDT", "SCALP", "LONG", 90, tp=100, sl=85, entry_mode="RETEST_WAIT"
    )
    # Backdate creation beyond the SCALP timeout (2h) so it expires.
    pending = store.read("pending_signals")
    pending[0]["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    store.write("pending_signals", pending)

    now_ms = int(time.time() * 1000)
    fake_client.klines["ADAUSDT"] = _candles_after(now_ms - 6 * 3600_000, [
        (95, 99, 92, 98),  # never trades down to the 90 entry
    ])
    resolved = tracker.check_pending()
    assert resolved[0].status == Status.INVALIDATED.value
    assert resolved[0].pnl_pct == 0.0


def test_bad_symbol_does_not_wedge_batch(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    good = tracker.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95, entry_mode="MOMENTUM_NOW"
    )
    tracker.record_signal(
        "BADUSDT", "SCREENER", "LONG", 100, tp=110, sl=95, entry_mode="MOMENTUM_NOW"
    )
    now_ms = int(datetime.fromisoformat(good.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 111, 99, 110)])
    # BADUSDT has no klines -> stays pending, must not break BTC resolution.
    resolved = tracker.check_pending()
    assert {r.symbol for r in resolved} == {"BTCUSDT"}
    assert len(tracker.active_signals()) == 1  # BADUSDT still pending


# ── stats ──────────────────────────────────────────────────────────────────
def test_stats_win_rate(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    # One win, one loss recorded as outcomes directly.
    win = tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95, entry_mode="MOMENTUM_NOW")
    now_ms = int(datetime.fromisoformat(win.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 111, 99, 110)])
    tracker.check_pending()

    loss = tracker.record_signal("ETHUSDT", "SCREENER", "LONG", 100, tp=110, sl=95, entry_mode="MOMENTUM_NOW")
    now_ms = int(datetime.fromisoformat(loss.created_at).timestamp() * 1000)
    fake_client.klines["ETHUSDT"] = _candles_after(now_ms, [(100, 101, 94, 96)])
    tracker.check_pending()

    stats = tracker.stats()
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == 50.0
    assert "by_ai_verdict" in stats
    assert "vetoed_win_rate" in stats


def test_stats_ai_verdict_breakdown(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)

    # Win flagged CONFIRM by AI
    sig = tracker.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", ai_verdict="CONFIRM", ai_confidence=85,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 111, 99, 110)])
    tracker.check_pending()

    # Loss flagged REJECT (ai_vetoed=True) by AI
    sig2 = tracker.record_signal(
        "ETHUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", ai_verdict="REJECT", ai_confidence=80, ai_vetoed=True,
    )
    now_ms2 = int(datetime.fromisoformat(sig2.created_at).timestamp() * 1000)
    fake_client.klines["ETHUSDT"] = _candles_after(now_ms2, [(100, 101, 94, 96)])
    tracker.check_pending()

    stats = tracker.stats()
    by_ai = stats["by_ai_verdict"]
    assert by_ai["CONFIRM"]["wins"] == 1 and by_ai["CONFIRM"]["total"] == 1
    assert by_ai["REJECT"]["wins"] == 0 and by_ai["REJECT"]["total"] == 1
    assert stats["vetoed_count"] == 1
    assert stats["vetoed_win_rate"] == 0.0


# ── trade report payload (paper account + lesson) ───────────────────────────
def test_resolution_info_includes_balance_and_lesson(store, fake_client, tracker_settings):
    from wolf.account import PaperAccount

    events = []
    account = PaperAccount(store, start_balance=1000.0, risk_pct=1.0)
    tracker = Tracker(
        store, fake_client, tracker_settings,
        notify=lambda sig, event, info: events.append((event, info)),
        account=account,
    )
    sig = tracker.record_signal(
        "BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", strategy="MOMENTUM",
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 111, 99, 110)])
    tracker.check_pending()

    resolved = [info for ev, info in events if ev == "RESOLVED"]
    assert resolved, "expected a RESOLVED notification"
    info = resolved[0]
    # +10% on a 5% stop = +2R; risk 1% of 1000 = 10 => +20 -> balance 1020.
    assert info["r_multiple"] == 2.0
    assert info["balance"] == 1020.0
    assert "lesson" in info and "MOMENTUM" in info["lesson"]


# ── concurrency ────────────────────────────────────────────────────────────
def test_concurrent_records_do_not_lose_signals(store, fake_client, tracker_settings):
    """Many threads recording distinct signals must not clobber one another."""
    tracker = Tracker(store, fake_client, tracker_settings)

    def record(n: int):
        tracker.record_signal(f"SYM{n}USDT", "SCREENER", "LONG", 100, tp=110, sl=95)

    threads = [threading.Thread(target=record, args=(n,)) for n in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(tracker.active_signals()) == 40


def test_record_concurrent_with_check_pending(store, fake_client, tracker_settings):
    """record_signal racing check_pending must not drop the new signal."""
    tracker = Tracker(store, fake_client, tracker_settings)
    # Seed one pending signal with no candle data (stays pending on check).
    tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95)

    def checker():
        for _ in range(20):
            tracker.check_pending()

    def recorder():
        for n in range(20):
            tracker.record_signal(f"ALT{n}USDT", "SCREENER", "LONG", 100, tp=110, sl=95)

    threads = [threading.Thread(target=checker), threading.Thread(target=recorder)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BTC + 20 ALT signals, none lost to a race.
    assert len(tracker.active_signals()) == 21


# ── emit count tracking ─────────────────────────────────────────────────────
def test_stats_emit_count_per_strategy(store, fake_client, tracker_settings):
    """by_strategy['emitted'] counts all recorded signals, not just graded ones."""
    tracker = Tracker(store, fake_client, tracker_settings)
    # Two SCALP signals — different symbols so dedup doesn't block them.
    tracker.record_signal(
        "BTCUSDT", "SCALP", "LONG", 100, tp=110, sl=95,
        entry_mode="MOMENTUM_NOW", strategy="SCALP",
    )
    tracker.record_signal(
        "ETHUSDT", "SCALP", "SHORT", 100, tp=90, sl=105,
        entry_mode="MOMENTUM_NOW", strategy="SCALP",
    )
    stats = tracker.stats()
    scalp = stats["by_strategy"].get("SCALP", {})
    assert scalp.get("emitted", 0) == 2    # both recorded
    assert scalp.get("active", 0) == 2     # both still pending
    assert scalp.get("total", 0) == 0      # none graded yet


# ── regression: pre-activation candles must not grade a position ────────────
def test_pre_activation_candles_cannot_hit_tp(store, fake_client, tracker_settings):
    """A retest entry must not inherit TP hits from before it went live.

    Every cycle replays from created_at, so once ``activated`` was persisted the
    replay treated the pre-retest candles as live. For a LONG that waits for a
    dip, those candles sit above entry — right where the TP rungs are — so the
    second cycle booked a phantom TP_HIT.
    """
    tracker = Tracker(store, fake_client, tracker_settings)
    sig = tracker.record_signal(
        "ADAUSDT", "SWING", "LONG", 90, tp=100, sl=85,
        entry_mode="RETEST_WAIT", tps=[{"level": 1, "price": 95}, {"level": 2, "price": 100}],
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["ADAUSDT"] = _candles_after(now_ms, [
        (98, 101, 96, 97),  # above entry throughout: sweeps past TP1 *and* TP2
        (94, 94, 89, 91),   # dips to 90 -> entry finally touched, no TP in range
    ])

    assert tracker.check_pending() == []  # activated, nothing terminal yet
    active = tracker.active_signals()
    assert len(active) == 1 and active[0].tps_hit == []

    # Second cycle over the same history must reach the same conclusion.
    assert tracker.check_pending() == []
    assert tracker.active_signals()[0].tps_hit == []


# ── grading in R ────────────────────────────────────────────────────────────
def _age_pending(store, hours: float) -> None:
    pending = store.read("pending_signals")
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    pending[0]["created_at"] = old
    if pending[0].get("activated_at"):
        pending[0]["activated_at"] = old
    store.write("pending_signals", pending)


def test_r_multiple_normalises_pnl_by_risk(store, fake_client, tracker_settings):
    """The same -1R loss on two coins of very different volatility."""
    tracker = Tracker(store, fake_client, tracker_settings)
    for sym, entry, sl, low in (("QUIET", 100, 99.7, 99.6), ("WILD", 100, 97.0, 96.9)):
        tracker.record_signal(sym, "SCREENER", "LONG", entry, tp=entry * 1.1, sl=sl,
                              entry_mode="MOMENTUM_NOW")
        created = [s for s in tracker.active_signals() if s.symbol == sym][0].created_at
        now_ms = int(datetime.fromisoformat(created).timestamp() * 1000)
        fake_client.klines[sym] = _candles_after(now_ms, [(entry, entry, low, low)])

    resolved = {s.symbol: s for s in tracker.check_pending()}
    assert resolved["QUIET"].pnl_pct == -0.3     # wildly different percentages...
    assert resolved["WILD"].pnl_pct == -3.0
    assert resolved["QUIET"].r_multiple == -1.0  # ...identical risk-adjusted damage
    assert resolved["WILD"].r_multiple == -1.0


def test_r_multiple_derived_for_pre_existing_outcomes(store, fake_client, tracker_settings):
    """Outcomes booked before r_multiple existed still report in R.

    entry and sl are persisted, so the conversion is recoverable on read — no
    backfill migration needed for the outcome log already on disk.
    """
    from wolf.tracker import r_multiple_of

    tracker = Tracker(store, fake_client, tracker_settings)
    sig = tracker.record_signal("ETHUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
                                entry_mode="MOMENTUM_NOW")
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["ETHUSDT"] = _candles_after(now_ms, [(100, 101, 94, 96)])
    tracker.check_pending()

    outcomes = store.read("signal_outcomes")
    outcomes[0].pop("r_multiple")  # as an old record would look
    store.write("signal_outcomes", outcomes)

    assert r_multiple_of(tracker.outcomes()[0]) == -1.0
    assert tracker.stats()["avg_r"] == -1.0


def test_expiry_inside_dead_band_carries_no_verdict(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    tracker.record_signal("SOLUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
                          entry_mode="MOMENTUM_NOW")
    _age_pending(store, 30)  # past the 24h SCREENER timeout
    now_ms = int(time.time() * 1000)
    fake_client.klines["SOLUSDT"] = _candles_after(now_ms - 31 * 3600_000, [(100, 101, 99, 100)])
    fake_client.prices["SOLUSDT"] = 100.1  # +0.1% on 5% risk = 0.02R, pure noise

    resolved = tracker.check_pending()
    assert resolved[0].status == Status.EXPIRED_FLAT.value
    stats = tracker.stats()
    assert stats["wins"] == 0 and stats["losses"] == 0
    assert stats["flat"] == 1
    assert stats["total_traded"] == 1  # still a trade for expectancy purposes


def test_expiry_beyond_dead_band_still_grades(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    tracker.record_signal("BNBUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
                          entry_mode="MOMENTUM_NOW")
    _age_pending(store, 30)
    now_ms = int(time.time() * 1000)
    fake_client.klines["BNBUSDT"] = _candles_after(now_ms - 31 * 3600_000, [(100, 101, 99, 100)])
    fake_client.prices["BNBUSDT"] = 102.0  # +2% on 5% risk = 0.4R

    resolved = tracker.check_pending()
    assert resolved[0].status == Status.EXPIRED_WIN.value
    assert resolved[0].r_multiple == 0.4


def test_stats_window_excludes_older_outcomes(store, fake_client, tracker_settings):
    """A 24h report must describe that day, not every day since startup."""
    tracker = Tracker(store, fake_client, tracker_settings)
    sig = tracker.record_signal("BTCUSDT", "SCREENER", "LONG", 100, tp=110, sl=95,
                                entry_mode="MOMENTUM_NOW")
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = _candles_after(now_ms, [(100, 111, 99, 110)])
    tracker.check_pending()

    outcomes = store.read("signal_outcomes")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    outcomes[0]["resolved_at"] = outcomes[0]["exit_time"] = old
    store.write("signal_outcomes", outcomes)

    assert tracker.stats()["total_traded"] == 1               # all-time sees it
    assert tracker.stats(window_hours=24)["total_traded"] == 0  # yesterday does not


def test_stats_reports_spread_of_r_not_just_the_average(store, fake_client, tracker_settings):
    """The gate needs to know how noisy an average is before acting on it."""
    tracker = Tracker(store, fake_client, tracker_settings)
    # Two -1R losses and one +2R win, all on the same 5% risk.
    for sym, high, low in (("A", 101, 94), ("B", 101, 94), ("C", 111, 99)):
        tracker.record_signal(sym, "SCREENER", "LONG", 100, tp=110, sl=95,
                              entry_mode="MOMENTUM_NOW")
        created = [s for s in tracker.active_signals() if s.symbol == sym][0].created_at
        now_ms = int(datetime.fromisoformat(created).timestamp() * 1000)
        fake_client.klines[sym] = _candles_after(now_ms, [(100, high, low, 100)])
    tracker.check_pending()

    b = tracker.stats()["by_strategy"]["CONFIRMED"]
    assert b["total"] == 3
    assert b["avg_r"] == 0.0            # (-1 -1 +2) / 3
    assert b["sd_r"] > 1.0              # ...but wildly spread, so it means nothing
    assert b["se_r"] == round(b["sd_r"] / 3 ** 0.5, 3)
