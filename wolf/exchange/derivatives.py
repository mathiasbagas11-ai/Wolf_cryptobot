"""Funding-rate sources (perpetual swaps).

Funding rate is the key derivatives input for the PREPUMP (crowded shorts) and
PREDUMP (overheated longs) detectors. Like price/candles, it can come from
several venues; modelling each as a :class:`FundingSource` lets
:class:`~wolf.exchange.client.MarketDataClient` fall back across them so funding
keeps flowing even when one venue's futures API is geo-blocked.

All sources return the latest funding rate as a **percent** (e.g. -0.05), so it
matches the thresholds in :mod:`wolf.market`. Open-interest change stays
Binance-specific (on :class:`~wolf.exchange.binance.BinanceClient`); when no
funding/OI is available the detectors degrade to candle-only.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests

from wolf.exchange.sources import split_quote

log = logging.getLogger("wolf.exchange")


class FundingSource(ABC):
    name: str = "base"

    def __init__(self, timeout: float = 10.0, session: Optional[requests.Session] = None) -> None:
        self._timeout = timeout
        self._session = session or requests.Session()

    def _get_json(self, url: str, params: dict):
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.debug("%s funding HTTP error: %s", self.name, exc)
        except ValueError as exc:
            log.debug("%s funding invalid JSON: %s", self.name, exc)
        return None

    @abstractmethod
    def _request(self, symbol: str) -> tuple[str, dict]:
        ...

    @abstractmethod
    def parse(self, payload) -> Optional[float]:
        """Parse a decoded payload into a funding rate in percent."""

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        url, params = self._request(symbol)
        payload = self._get_json(url, params)
        if payload is None:
            return None
        try:
            return self.parse(payload)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.debug("%s funding parse failed for %s: %s", self.name, symbol, exc)
            return None


class BinanceFunding(FundingSource):
    name = "binance"

    def __init__(self, base_url: str = "https://fapi.binance.com", **kw) -> None:
        super().__init__(**kw)
        self._base = base_url.rstrip("/")

    def _request(self, symbol):
        return f"{self._base}/fapi/v1/premiumIndex", {"symbol": symbol}

    def parse(self, payload) -> Optional[float]:
        return float(payload["lastFundingRate"]) * 100 if isinstance(payload, dict) else None


class OKXFunding(FundingSource):
    name = "okx"

    def __init__(self, base_url: str = "https://www.okx.com", **kw) -> None:
        super().__init__(**kw)
        self._base = base_url.rstrip("/")

    def _request(self, symbol):
        base, quote = split_quote(symbol)
        inst = f"{base}-{quote}-SWAP" if quote else symbol
        return f"{self._base}/api/v5/public/funding-rate", {"instId": inst}

    def parse(self, payload) -> Optional[float]:
        rows = payload.get("data") if isinstance(payload, dict) else None
        return float(rows[0]["fundingRate"]) * 100 if rows else None


class BybitFunding(FundingSource):
    name = "bybit"

    def __init__(self, base_url: str = "https://api.bybit.com", **kw) -> None:
        super().__init__(**kw)
        self._base = base_url.rstrip("/")

    def _request(self, symbol):
        return f"{self._base}/v5/market/tickers", {"category": "linear", "symbol": symbol}

    def parse(self, payload) -> Optional[float]:
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        if not rows:
            return None
        rate = rows[0].get("fundingRate")
        return float(rate) * 100 if rate not in (None, "") else None
