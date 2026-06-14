"""Exchange data sources.

Each :class:`ExchangeSource` fetches OHLC candles and the spot price for a symbol
from one exchange, normalising the venue-specific symbol format, interval codes
and JSON payload into the common :class:`~wolf.models.Candle` shape. This is the
fallback unit the old bot's ``exchange_resolver`` provided — one source per
venue, all behind one interface so :class:`~wolf.exchange.client.MarketDataClient`
can try them in order.

Parsing is split into a pure ``parse_klines`` method (no I/O) so it can be unit
tested with canned payloads even when the network is unavailable.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests

from wolf.models import Candle

log = logging.getLogger("wolf.exchange")


class ExchangeSource(ABC):
    """One venue's klines + price, normalised to common types."""

    name: str = "base"

    def __init__(self, base_url: str, timeout: float = 10.0, session: Optional[requests.Session] = None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    # ── venue-specific mappings (override) ──
    @abstractmethod
    def _symbol(self, symbol: str) -> str:
        """Map a canonical ``BTCUSDT`` to this venue's symbol format."""

    @abstractmethod
    def _interval(self, interval: str) -> str:
        """Map a canonical interval (e.g. ``15m``) to this venue's code."""

    @abstractmethod
    def _klines_request(self, symbol: str, interval: str, limit: int) -> tuple[str, dict]:
        """Return the ``(url, params)`` for a klines request."""

    @abstractmethod
    def parse_klines(self, payload) -> list[Candle]:
        """Parse a decoded klines payload into ascending-time candles."""

    @abstractmethod
    def _price_request(self, symbol: str) -> tuple[str, dict]:
        """Return the ``(url, params)`` for a price request."""

    @abstractmethod
    def parse_price(self, payload) -> Optional[float]:
        """Parse a decoded ticker payload into a float price."""

    # ── shared HTTP + public API ──
    def _get_json(self, url: str, params: dict):
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.debug("%s HTTP error %s %s: %s", self.name, url, params, exc)
        except ValueError as exc:
            log.debug("%s invalid JSON %s: %s", self.name, url, exc)
        return None

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list[Candle]:
        limit = max(1, min(1000, limit))
        url, params = self._klines_request(self._symbol(symbol), self._interval(interval), limit)
        payload = self._get_json(url, params)
        if payload is None:
            return []
        try:
            return self.parse_klines(payload)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.debug("%s kline parse failed for %s: %s", self.name, symbol, exc)
            return []

    def get_price(self, symbol: str) -> Optional[float]:
        url, params = self._price_request(self._symbol(symbol))
        payload = self._get_json(url, params)
        if payload is None:
            return None
        try:
            return self.parse_price(payload)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.debug("%s price parse failed for %s: %s", self.name, symbol, exc)
            return None


def _split_quote(symbol: str) -> tuple[str, str]:
    """Split ``BTCUSDT`` into ``(BTC, USDT)``; falls back to (sym, '')."""
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


class BinanceSource(ExchangeSource):
    name = "binance"

    def __init__(self, base_url: str = "https://api.binance.com/api/v3", **kw) -> None:
        super().__init__(base_url, **kw)

    def _symbol(self, symbol: str) -> str:
        return symbol

    def _interval(self, interval: str) -> str:
        return interval  # native: 15m, 1h, 4h, 1d

    def _klines_request(self, symbol, interval, limit):
        return f"{self._base}/klines", {"symbol": symbol, "interval": interval, "limit": limit}

    def parse_klines(self, payload) -> list[Candle]:
        if not isinstance(payload, list):
            return []
        return [Candle.from_binance(row) for row in payload]

    def _price_request(self, symbol):
        return f"{self._base}/ticker/price", {"symbol": symbol}

    def parse_price(self, payload) -> Optional[float]:
        return float(payload["price"]) if isinstance(payload, dict) else None


class OKXSource(ExchangeSource):
    name = "okx"
    _INTERVALS = {"15m": "15m", "1m": "1m", "5m": "5m", "30m": "30m",
                  "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D"}

    def __init__(self, base_url: str = "https://www.okx.com", **kw) -> None:
        super().__init__(base_url, **kw)

    def _symbol(self, symbol: str) -> str:
        base, quote = _split_quote(symbol)
        return f"{base}-{quote}" if quote else symbol

    def _interval(self, interval: str) -> str:
        return self._INTERVALS.get(interval, "15m")

    def _klines_request(self, symbol, interval, limit):
        return f"{self._base}/api/v5/market/candles", {"instId": symbol, "bar": interval, "limit": limit}

    def parse_klines(self, payload) -> list[Candle]:
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not rows:
            return []
        # OKX returns newest-first; reverse to ascending time.
        candles = [
            Candle(time=int(r[0]), open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in reversed(rows)
        ]
        return candles

    def _price_request(self, symbol):
        return f"{self._base}/api/v5/market/ticker", {"instId": symbol}

    def parse_price(self, payload) -> Optional[float]:
        rows = payload.get("data") if isinstance(payload, dict) else None
        return float(rows[0]["last"]) if rows else None


class BybitSource(ExchangeSource):
    name = "bybit"
    _INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30",
                  "1h": "60", "2h": "120", "4h": "240", "1d": "D"}

    def __init__(self, base_url: str = "https://api.bybit.com", category: str = "spot", **kw) -> None:
        super().__init__(base_url, **kw)
        self._category = category

    def _symbol(self, symbol: str) -> str:
        return symbol  # native: BTCUSDT

    def _interval(self, interval: str) -> str:
        return self._INTERVALS.get(interval, "15")

    def _klines_request(self, symbol, interval, limit):
        return f"{self._base}/v5/market/kline", {
            "category": self._category, "symbol": symbol, "interval": interval, "limit": limit
        }

    def parse_klines(self, payload) -> list[Candle]:
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        if not rows:
            return []
        # Bybit returns newest-first; reverse to ascending time.
        return [
            Candle(time=int(r[0]), open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in reversed(rows)
        ]

    def _price_request(self, symbol):
        return f"{self._base}/v5/market/tickers", {"category": self._category, "symbol": symbol}

    def parse_price(self, payload) -> Optional[float]:
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        return float(rows[0]["lastPrice"]) if rows else None
