"""Multi-exchange market-data client with automatic fallback.

Composes an ordered list of :class:`~wolf.exchange.sources.ExchangeSource`
objects (e.g. Binance -> OKX -> Bybit) and serves klines/price from the first
one that responds, remembering the winning source per symbol so subsequent
cycles skip dead venues. This is the clean re-implementation of the old bot's
``exchange_resolver`` — resilient to a single venue being geo-blocked or down.

Derivatives data (funding rate, open-interest change) is venue-specific and
delegated to an optional ``futures`` provider (Binance futures). When it's
unavailable the market context simply omits funding/OI — detectors degrade to
candle-only, by design.

Exposes the same method surface the tracker/screener/context already depend on
(``get_klines``, ``get_price``, ``get_funding_rate``, ``get_open_interest_change``),
so it is a drop-in replacement for the single-venue ``BinanceClient``.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Sequence

from wolf.exchange.sources import ExchangeSource
from wolf.models import Candle

log = logging.getLogger("wolf.exchange")


class MarketDataClient:
    def __init__(self, sources: Sequence[ExchangeSource], futures=None, funding_sources=None) -> None:
        if not sources:
            raise ValueError("MarketDataClient needs at least one source")
        self._sources = list(sources)
        self._futures = futures  # provides open-interest change (Binance)
        self._funding_sources = list(funding_sources) if funding_sources else []
        self._preferred: dict[str, str] = {}  # symbol -> source name that last worked
        self._lock = threading.Lock()

    @property
    def source_names(self) -> list[str]:
        return [s.name for s in self._sources]

    def _ordered(self, symbol: str) -> list[ExchangeSource]:
        """Sources to try, preferred (last-working) one first."""
        with self._lock:
            pref = self._preferred.get(symbol)
        if not pref:
            return self._sources
        return sorted(self._sources, key=lambda s: 0 if s.name == pref else 1)

    def _remember(self, symbol: str, name: str) -> None:
        with self._lock:
            if self._preferred.get(symbol) != name:
                self._preferred[symbol] = name

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list[Candle]:
        for source in self._ordered(symbol):
            candles = source.get_klines(symbol, interval, limit)
            if candles:
                self._remember(symbol, source.name)
                return candles
        log.warning("No exchange served klines for %s %s", symbol, interval)
        return []

    def get_price(self, symbol: str) -> Optional[float]:
        for source in self._ordered(symbol):
            price = source.get_price(symbol)
            if price is not None:
                self._remember(symbol, source.name)
                return price
        return None

    # ── market-wide / trades (for reports) ──
    def get_market_overview(self) -> list[dict]:
        """All-symbols 24h snapshot from the first venue that supports it."""
        for source in self._sources:
            data = source.get_24h_overview()
            if data:
                return data
        return []

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[dict]:
        for source in self._ordered(symbol):
            trades = source.get_recent_trades(symbol, limit)
            if trades:
                self._remember(symbol, source.name)
                return trades
        return []

    # ── derivatives ──
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        # Try the venue fallback list first (Binance -> OKX -> Bybit), then the
        # futures provider as a last resort. Funding is optional: None just means
        # detectors run candle-only.
        for source in self._funding_sources:
            rate = source.get_funding_rate(symbol)
            if rate is not None:
                return rate
        if self._futures:
            return self._futures.get_funding_rate(symbol)
        return None

    def get_open_interest_change(self, symbol: str, period: str = "5m", limit: int = 12) -> Optional[float]:
        return self._futures.get_open_interest_change(symbol, period, limit) if self._futures else None
