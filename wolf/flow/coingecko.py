"""CoinGecko market data — the free, key-less token-level metrics.

Provides the numbers the flow-intelligence report needs for its TOKEN PICKS and
SKIP sections: market cap, fully-diluted valuation (→ FDV/MC unlock pressure),
24h volume (→ volume-vs-mcap liquidity proxy), and price change. Parsing is a
pure method so it unit-tests against canned payloads without the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("wolf.flow")


@dataclass(frozen=True)
class TokenMetrics:
    symbol: str
    name: str
    price: float
    change_24h: float          # percent
    market_cap: float
    fdv: float                 # fully-diluted valuation
    volume_24h: float
    ath_change_pct: float = 0.0   # percent from all-time high (negative = below ATH)

    @property
    def fdv_mc(self) -> Optional[float]:
        """FDV / market-cap ratio — proxy for outstanding unlock pressure."""
        if not self.market_cap or not self.fdv:
            return None
        return self.fdv / self.market_cap

    @property
    def vol_mc(self) -> Optional[float]:
        """24h volume / market-cap — liquidity / turnover proxy."""
        if not self.market_cap:
            return None
        return self.volume_24h / self.market_cap


@dataclass(frozen=True)
class GlobalMetrics:
    btc_dominance: float           # percent
    total_market_cap: float        # usd
    market_cap_change_24h: float   # percent


class CoinGeckoClient:
    name = "coingecko"

    def __init__(self, base_url: str = "https://api.coingecko.com/api/v3",
                 timeout: float = 10.0, session: Optional[requests.Session] = None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    def _get(self, path: str, params: dict) -> Optional[object]:
        try:
            resp = self._session.get(f"{self._base}{path}", params=params,
                                     timeout=self._timeout, headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("coingecko %s error: %s", path, exc)
            return None

    # ── raw fetch ──────────────────────────────────────────────────────
    def top_markets(self, limit: int = 50) -> list[TokenMetrics]:
        payload = self._get("/coins/markets", {
            "vs_currency": "usd",
            "order": "volume_desc",
            "per_page": limit,
            "page": 1,
            "price_change_percentage": "24h",
        })
        return self.parse_markets(payload)

    def global_data(self) -> Optional[GlobalMetrics]:
        payload = self._get("/global", {})
        return self.parse_global(payload)

    # ── pure parsing (testable without network) ────────────────────────
    @staticmethod
    def parse_markets(payload) -> list[TokenMetrics]:
        if not isinstance(payload, list):
            return []
        out: list[TokenMetrics] = []
        for r in payload:
            if not isinstance(r, dict):
                continue
            out.append(TokenMetrics(
                symbol=str(r.get("symbol", "")).upper(),
                name=str(r.get("name", "")),
                price=_f(r.get("current_price")),
                change_24h=_f(r.get("price_change_percentage_24h")),
                market_cap=_f(r.get("market_cap")),
                fdv=_f(r.get("fully_diluted_valuation")),
                volume_24h=_f(r.get("total_volume")),
                ath_change_pct=_f(r.get("ath_change_percentage")),
            ))
        return out

    @staticmethod
    def parse_global(payload) -> Optional[GlobalMetrics]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        dom = data.get("market_cap_percentage", {})
        total = data.get("total_market_cap", {})
        return GlobalMetrics(
            btc_dominance=_f(dom.get("btc") if isinstance(dom, dict) else 0),
            total_market_cap=_f(total.get("usd") if isinstance(total, dict) else 0),
            market_cap_change_24h=_f(data.get("market_cap_change_percentage_24h_usd")),
        )


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
