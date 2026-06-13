"""Binance market-data client.

A thin, well-typed wrapper over the public Binance REST endpoints used by the
tracker. Network and parsing failures are caught *narrowly* (``requests``
exceptions, ``KeyError``/``ValueError`` on the payload) and logged — never
swallowed by a bare ``except:``. Callers get ``None``/empty results on failure
and can decide what to do, which is what fixes the "silent bug" problem of the
old code's 350+ broad excepts.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from wolf.models import Candle

log = logging.getLogger("wolf.exchange")


class BinanceClient:
    def __init__(
        self,
        spot_base: str = "https://api.binance.com/api/v3",
        futures_base: str = "https://fapi.binance.com",
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._spot_base = spot_base.rstrip("/")
        self._futures_base = futures_base.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    def _get_json(self, url: str, params: dict):
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("HTTP error GET %s %s: %s", url, params, exc)
        except ValueError as exc:  # invalid JSON body
            log.warning("Invalid JSON from %s %s: %s", url, params, exc)
        return None

    def get_price(self, symbol: str) -> Optional[float]:
        """Return the latest spot price for ``symbol`` (e.g. ``BTCUSDT``)."""
        data = self._get_json(
            f"{self._spot_base}/ticker/price", {"symbol": symbol}
        )
        if not isinstance(data, dict):
            return None
        try:
            return float(data["price"])
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Unexpected ticker payload for %s: %s", symbol, exc)
            return None

    def get_klines(
        self, symbol: str, interval: str = "15m", limit: int = 100
    ) -> list[Candle]:
        """Return up to ``limit`` candles for ``symbol`` at ``interval``."""
        limit = max(1, min(1000, limit))
        data = self._get_json(
            f"{self._spot_base}/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(data, list):
            return []
        candles: list[Candle] = []
        for row in data:
            try:
                candles.append(Candle.from_binance(row))
            except (IndexError, ValueError, TypeError) as exc:
                log.debug("Skipping malformed kline for %s: %s", symbol, exc)
        return candles

    def get_24h_stats(self, symbol: str) -> Optional[dict]:
        """Return 24h rolling stats (priceChangePercent, quoteVolume, ...)."""
        data = self._get_json(
            f"{self._spot_base}/ticker/24hr", {"symbol": symbol}
        )
        return data if isinstance(data, dict) else None
