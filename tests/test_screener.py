"""Tests for the screening orchestration."""

from __future__ import annotations

from wolf.detectors import MomentumBreakoutDetector
from wolf.models import Candle
from wolf.screener import Screener
from wolf.tracker import Tracker


def _breakout_candles() -> list[Candle]:
    candles = [Candle(time=i * 900_000, open=100, high=101, low=99, close=100, volume=100.0) for i in range(60)]
    candles.append(Candle(time=60 * 900_000, open=100, high=108, low=100, close=107, volume=500.0))
    return candles


def test_run_cycle_records_signal(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None, universe=["BTCUSDT"]
    )
    recorded = screener.run_cycle()
    assert len(recorded) == 1
    assert recorded[0].symbol == "BTCUSDT"
    assert len(tracker.active_signals()) == 1


def test_run_cycle_no_data_records_nothing(store, fake_client, tracker_settings):
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(
        fake_client, tracker, [MomentumBreakoutDetector()], notifier=None, universe=["BTCUSDT"]
    )
    assert screener.run_cycle() == []


def test_scan_symbol_picks_highest_score(store, fake_client, tracker_settings):
    fake_client.klines["BTCUSDT"] = _breakout_candles()
    tracker = Tracker(store, fake_client, tracker_settings)
    screener = Screener(fake_client, tracker, [MomentumBreakoutDetector()], universe=["BTCUSDT"])
    candidate = screener.scan_symbol("BTCUSDT")
    assert candidate is not None and candidate.direction == "LONG"
