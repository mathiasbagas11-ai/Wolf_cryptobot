"""Market sentiment & institutional-demand proxies — free, key-less.

Ports two signals from the previous bot that map directly onto the flow-thread
opening ("Extreme Fear di permukaan, tapi smart money positioning", "institusi
baru beli"):

* **Fear & Greed Index** (alternative.me) — crowd mood; contrarian fuel when
  extreme fear meets institutional accumulation.
* **Coinbase Premium** — BTC/USD on Coinbase (US institutional venue) vs
  BTC/USDT on Binance. A positive premium = US institutions bidding; negative =
  distributing. Pure price math, no key.

Parsing is split from fetching so both unit-test against canned payloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("wolf.flow")

# Coinbase premium thresholds (percent) — institutional accumulation/distribution.
PREMIUM_ACCUMULATION = 0.05
PREMIUM_DISTRIBUTION = -0.05


@dataclass(frozen=True)
class FearGreed:
    value: int                 # 0–100
    classification: str        # Extreme Fear / Fear / Neutral / Greed / Extreme Greed

    @property
    def is_fear(self) -> bool:
        return self.value <= 45

    @property
    def is_greed(self) -> bool:
        return self.value >= 60


@dataclass(frozen=True)
class CoinbasePremium:
    premium_pct: float
    cb_price: float
    bn_price: float

    @property
    def signal(self) -> str:
        if self.premium_pct >= PREMIUM_ACCUMULATION:
            return "ACCUMULATION"   # US institutions bidding
        if self.premium_pct <= PREMIUM_DISTRIBUTION:
            return "DISTRIBUTION"   # US institutions selling
        return "NEUTRAL"


class SentimentClient:
    name = "sentiment"

    def __init__(self, timeout: float = 10.0, session: Optional[requests.Session] = None) -> None:
        self._timeout = timeout
        self._session = session or requests.Session()

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[object]:
        try:
            resp = self._session.get(url, params=params or {}, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0", "Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("sentiment %s error: %s", url, exc)
            return None

    # ── Fear & Greed ───────────────────────────────────────────────────
    def fear_greed(self) -> Optional[FearGreed]:
        payload = self._get("https://api.alternative.me/fng/", {"limit": 1, "format": "json"})
        return self.parse_fear_greed(payload)

    @staticmethod
    def parse_fear_greed(payload) -> Optional[FearGreed]:
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not rows:
            return None
        row = rows[0]
        try:
            return FearGreed(value=int(row.get("value", 50)),
                             classification=str(row.get("value_classification", "Neutral")))
        except (TypeError, ValueError):
            return None

    # ── Coinbase Premium ───────────────────────────────────────────────
    def coinbase_premium(self) -> Optional[CoinbasePremium]:
        cb = self._coinbase_btc_usd()
        bn = self._binance_btc_usdt()
        if not cb or not bn:
            return None
        return CoinbasePremium(premium_pct=(cb / bn - 1) * 100, cb_price=cb, bn_price=bn)

    def _coinbase_btc_usd(self) -> Optional[float]:
        data = self._get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        if isinstance(data, dict):
            try:
                price = float(data.get("data", {}).get("amount", 0))
                if price > 0:
                    return price
            except (TypeError, ValueError):
                pass
        # Fallback: Coinbase Exchange public ticker.
        data = self._get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
        if isinstance(data, dict):
            try:
                price = float(data.get("price", 0))
                if price > 0:
                    return price
            except (TypeError, ValueError):
                pass
        return None

    def _binance_btc_usdt(self) -> Optional[float]:
        data = self._get("https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        if isinstance(data, dict):
            try:
                price = float(data.get("price", 0))
                if price > 0:
                    return price
            except (TypeError, ValueError):
                pass
        return None
