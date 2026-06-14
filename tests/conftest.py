"""Shared test fixtures and fakes."""

from __future__ import annotations

from typing import Optional

import pytest

from wolf.config import Settings, TrackerSettings
from wolf.models import Candle
from wolf.state import StateStore
from wolf.tracker import Tracker


class FakeClient:
    """In-memory stand-in for :class:`~wolf.exchange.BinanceClient`.

    Lets tracker/screener tests run deterministically with no network.
    """

    def __init__(self) -> None:
        self.klines: dict[str, list[Candle]] = {}
        self.prices: dict[str, float] = {}

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list[Candle]:
        return list(self.klines.get(symbol, []))[-limit:]

    def get_price(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol)

    def get_24h_stats(self, symbol: str):
        return None


def make_candles(prices: list[tuple[float, float, float, float]], start_ms: int = 0, step_ms: int = 900_000):
    """Build candles from (open, high, low, close) tuples spaced 15m apart."""
    out = []
    for i, (o, h, l, c) in enumerate(prices):
        out.append(Candle(time=start_ms + i * step_ms, open=o, high=h, low=l, close=c, volume=100.0))
    return out


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state"))


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def tracker_settings() -> TrackerSettings:
    return TrackerSettings()


@pytest.fixture
def tracker(store, fake_client, tracker_settings) -> Tracker:
    return Tracker(store, fake_client, tracker_settings)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(state_dir=str(tmp_path / "state"))
