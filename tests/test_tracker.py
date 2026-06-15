"""Tests for the signal lifecycle tracker — the core of the bot."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from wolf.models import Candle, Status
from wolf.tracker import Tracker, normalize_ladder


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
