"""Coinbase premium — the US-institutional demand gauge.

BTC/USD on Coinbase (where US institutions execute) against BTC/USDT on Binance.
A positive spread means institutional bids are lifting the US book; a negative
one means they are distributing into it. Pure price arithmetic, no API key.

**Scope: BTC only.** For every other symbol the field is ``None`` and changes
nothing. That is a deliberate first cut, not an oversight: the premium is widely
read as a *market-wide* gauge, and it may well earn that role here — but wiring
it into every altcoin's gate on day one would make its effect impossible to
measure. BTC-only keeps the blast radius small enough to attribute.

Ported from ``coinbase_premium.py``, minus the parts that had grown past what
the data supports: the original's 0–100 "strength" score, the
``OVEREXTENDED_*``/``DIVERGENCE_*`` labels and the momentum adjustments were all
derived from at most twelve five-minute readings of a single spread. This keeps
the level, the raw prices and a three-way classification, and leaves the
interpreting to readers that have more than one input.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

log = logging.getLogger("wolf.onchain.premium")

#: StateStore document this collector owns.
STATE_KEY = "coinbase_premium"

# Percent thresholds. Mirrors wolf.flow.sentiment so the two never disagree
# about what "accumulation" means.
PREMIUM_ACCUMULATION = 0.05
PREMIUM_DISTRIBUTION = -0.05

ACCUMULATION = "ACCUMULATION"
DISTRIBUTION = "DISTRIBUTION"
NEUTRAL = "NEUTRAL"


# ── pure logic ────────────────────────────────────────────────────────────
def compute_premium_pct(coinbase_price: Optional[float],
                        binance_price: Optional[float]) -> Optional[float]:
    """``(CB / BN - 1) * 100``, or ``None`` when either side is missing."""
    if not coinbase_price or not binance_price or binance_price <= 0:
        return None
    return (coinbase_price / binance_price - 1) * 100


def classify_premium(premium_pct: Optional[float]) -> str:
    """Level → ``ACCUMULATION`` / ``DISTRIBUTION`` / ``NEUTRAL``."""
    if premium_pct is None:
        return NEUTRAL
    if premium_pct >= PREMIUM_ACCUMULATION:
        return ACCUMULATION
    if premium_pct <= PREMIUM_DISTRIBUTION:
        return DISTRIBUTION
    return NEUTRAL


def parse_coinbase_spot(payload: Any) -> Optional[float]:
    """Price from Coinbase's ``/v2/prices/BTC-USD/spot`` document."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    return _positive(data.get("amount"))


def parse_coinbase_ticker(payload: Any) -> Optional[float]:
    """Price from the Coinbase Exchange ``/products/BTC-USD/ticker`` document."""
    return _positive(payload.get("price")) if isinstance(payload, dict) else None


def parse_binance_price(payload: Any) -> Optional[float]:
    """Price from Binance's ``/api/v3/ticker/price`` document."""
    return _positive(payload.get("price")) if isinstance(payload, dict) else None


# ── collector (network) ───────────────────────────────────────────────────
class CoinbasePremiumCollector:
    """Fetches both legs of the spread and persists one BTC-scoped snapshot."""

    name = "coinbase_premium"

    #: The only symbol this collector speaks for.
    symbol = "BTC"

    def __init__(
        self,
        store,
        *,
        coinbase_spot_url: str = "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        coinbase_ticker_url: str = "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        binance_url: str = "https://api.binance.com/api/v3/ticker/price",
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._store = store
        self._cb_spot_url = coinbase_spot_url
        self._cb_ticker_url = coinbase_ticker_url
        self._binance_url = binance_url
        self._timeout = timeout
        self._session = session or requests.Session()

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[Any]:
        try:
            resp = self._session.get(
                url, params=params or {}, timeout=self._timeout,
                headers={"User-Agent": "wolf/1.0", "Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.debug("premium HTTP error %s: %s", url, exc)
        except ValueError as exc:
            log.debug("premium invalid JSON %s: %s", url, exc)
        return None

    def coinbase_price(self) -> Optional[float]:
        """Coinbase BTC/USD, falling back to the Exchange ticker."""
        price = parse_coinbase_spot(self._get(self._cb_spot_url))
        if price is not None:
            return price
        return parse_coinbase_ticker(self._get(self._cb_ticker_url))

    def binance_price(self) -> Optional[float]:
        return parse_binance_price(self._get(self._binance_url, {"symbol": "BTCUSDT"}))

    def collect(self) -> dict:
        """Fetch both legs, classify, persist. Never raises to the scheduler."""
        cb = self.coinbase_price()
        bn = self.binance_price()
        premium = compute_premium_pct(cb, bn)

        doc = {
            "symbol": self.symbol,
            "premium_pct": round(premium, 4) if premium is not None else None,
            "signal": classify_premium(premium),
            "coinbase_price": cb,
            "binance_price": bn,
            "available": premium is not None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._store.write(STATE_KEY, doc)
        if premium is None:
            log.debug("Coinbase premium unavailable (cb=%s bn=%s)", cb, bn)
        else:
            log.info("Coinbase premium %+.4f%% → %s", premium, doc["signal"])
        return doc


def _positive(v) -> Optional[float]:
    try:
        price = float(v)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None
