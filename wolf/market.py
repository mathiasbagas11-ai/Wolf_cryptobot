"""Market context — the per-symbol facts detectors and gates read.

Captures the futures-market signals (funding rate, open-interest momentum) the
original PREPUMP/PREDUMP detectors relied on, plus the on-chain, whale and
institutional-flow dimensions collected by :mod:`wolf.onchain`. Modelled as an
immutable value object that is built *once per symbol per cycle* by
:class:`ContextProvider` and passed into ``Detector.evaluate``. Keeping the data
separate from the fetching keeps detectors pure and unit-testable: a test
constructs a ``MarketContext(...)`` directly with no network.

The on-chain fields are a **lookup**, not a fetch. Collectors write snapshots to
the StateStore on their own schedule; this reads them. That is why the whale
scan can be global (one leaderboard read per scan) while the context stays
per-symbol — and why building a context costs no HTTP at all.

Thresholds mirror the previous bot:
* funding < -0.05%  -> short-squeeze potential (bullish for PREPUMP)
* funding < -0.10%  -> extreme short squeeze
* funding > +0.05%  -> longs overheated (bearish for PREDUMP)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from wolf.exchange.sources import split_quote

log = logging.getLogger("wolf.market")

FUNDING_SQUEEZE_THRESH = -0.05   # percent
FUNDING_EXTREME_THRESH = -0.10
FUNDING_OVERHEATED_THRESH = 0.05
OI_RISING_THRESH = 2.0           # percent change over the window

#: Default age past which a collected snapshot stops counting as current.
#: Beyond this the field reads ``None`` and the bot degrades to candle-only
#: behaviour, which it already handles. Gating a live signal on a stale whale
#: read is strictly worse than gating it on nothing.
DEFAULT_STALENESS_MIN = 30.0


@dataclass(frozen=True)
class MarketContext:
    funding_rate: Optional[float] = None   # percent
    oi_change_pct: Optional[float] = None   # percent

    # ── on-chain / whale / institutional dimensions ───────────────────
    # All optional: every one of them is absent when its collector is disabled,
    # has not run yet, or last ran too long ago to trust.
    onchain_bias: Optional[str] = None      # SUPPORTS_LONG | SUPPORTS_SHORT | NEUTRAL
    onchain_brief: str = ""                 # human-readable facts for the AI debate
    whale_coordination: Optional[str] = None    # "LONG" | "SHORT" | None
    whale_wallet_count: int = 0
    coinbase_premium_pct: Optional[float] = None   # BTC only; None elsewhere

    @property
    def funding_squeeze(self) -> bool:
        return self.funding_rate is not None and self.funding_rate < FUNDING_SQUEEZE_THRESH

    @property
    def funding_extreme_squeeze(self) -> bool:
        return self.funding_rate is not None and self.funding_rate < FUNDING_EXTREME_THRESH

    @property
    def funding_overheated_long(self) -> bool:
        return self.funding_rate is not None and self.funding_rate > FUNDING_OVERHEATED_THRESH

    @property
    def oi_rising(self) -> bool:
        return self.oi_change_pct is not None and self.oi_change_pct >= OI_RISING_THRESH

    @property
    def oi_falling(self) -> bool:
        return self.oi_change_pct is not None and self.oi_change_pct <= -OI_RISING_THRESH

    def whales_oppose(self, direction: str, min_wallets: int) -> bool:
        """True when ``min_wallets`` or more whales are positioned the other way.

        Used by the screener's veto gate. Deliberately a question the *caller*
        asks — the context states what the whales did, it does not decide what
        that means for a trade.
        """
        if self.whale_coordination is None or not direction:
            return False
        if self.whale_wallet_count < min_wallets:
            return False
        return self.whale_coordination.upper() != direction.upper()


def parse_iso(ts: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, assuming UTC when it carries no offset."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        log.debug("Unparseable state timestamp: %r", ts)
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def age_minutes(ts: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    """Age of an ISO timestamp in minutes, or ``None`` if it cannot be read."""
    parsed = parse_iso(ts)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return (reference - parsed).total_seconds() / 60.0


def is_fresh(doc: Any, max_age_min: float, *, now: Optional[datetime] = None) -> bool:
    """Whether a collected document is recent enough to act on.

    A document with no usable timestamp is treated as stale. Erring the other
    way would let an undated (or corrupt) snapshot gate signals forever.
    """
    if not isinstance(doc, dict):
        return False
    age = age_minutes(doc.get("ts"), now=now)
    return age is not None and age <= max_age_min


class ContextProvider:
    """Builds a :class:`MarketContext` for a symbol.

    Derivatives data comes from the exchange client (a live call); the on-chain
    dimensions are read out of the StateStore where the collectors left them. If
    no store is wired, or a collector has never run, the extra fields are simply
    absent and behaviour is exactly what it was before they existed.
    """

    def __init__(
        self,
        client,
        store=None,
        *,
        staleness_min: float = DEFAULT_STALENESS_MIN,
    ) -> None:
        self._client = client
        self._store = store
        self._staleness_min = staleness_min

    def build(self, symbol: str) -> MarketContext:
        funding = self._client.get_funding_rate(symbol)
        oi_change = self._client.get_open_interest_change(symbol)
        base = self.base_symbol(symbol)

        valuation = self._fresh("onchain_valuation")
        whale = self._fresh("whale_hyperliquid")
        premium = self._fresh("coinbase_premium")

        onchain_bias, onchain_brief = self._valuation_for(valuation, base)
        whale_direction, whale_wallets = self._whale_for(whale, base)

        return MarketContext(
            funding_rate=funding,
            oi_change_pct=oi_change,
            onchain_bias=onchain_bias,
            onchain_brief=onchain_brief,
            whale_coordination=whale_direction,
            whale_wallet_count=whale_wallets,
            coinbase_premium_pct=self._premium_for(premium, base),
        )

    @staticmethod
    def base_symbol(symbol: str) -> str:
        """``BTCUSDT`` → ``BTC``.

        Goes through :func:`~wolf.exchange.sources.split_quote` rather than the
        old ``.replace("USDT", "")``, which corrupted any base whose own name
        contains the quote string.
        """
        base, _ = split_quote((symbol or "").upper().strip())
        return base

    def _fresh(self, key: str) -> Optional[dict]:
        """Read a collected document, or ``None`` if missing or too old."""
        if self._store is None:
            return None
        doc = self._store.read(key, default=None)
        if not is_fresh(doc, self._staleness_min):
            if isinstance(doc, dict):
                log.debug("State '%s' is stale (>%.0fm) — treating as absent",
                          key, self._staleness_min)
            return None
        return doc

    @staticmethod
    def _valuation_for(doc: Optional[dict], base: str) -> tuple[Optional[str], str]:
        symbols = doc.get("symbols") if isinstance(doc, dict) else None
        row = symbols.get(base) if isinstance(symbols, dict) else None
        if not isinstance(row, dict):
            return None, ""
        bias = row.get("bias")
        return (str(bias) if bias else None), str(row.get("brief") or "")

    @staticmethod
    def _whale_for(doc: Optional[dict], base: str) -> tuple[Optional[str], int]:
        coins = doc.get("coins") if isinstance(doc, dict) else None
        row = coins.get(base) if isinstance(coins, dict) else None
        if not isinstance(row, dict):
            return None, 0
        direction = row.get("direction")
        try:
            count = int(row.get("wallet_count", 0))
        except (TypeError, ValueError):
            count = 0
        return (str(direction).upper() if direction else None), count

    @staticmethod
    def _premium_for(doc: Optional[dict], base: str) -> Optional[float]:
        """BTC only — other symbols get ``None`` and are unaffected."""
        if not isinstance(doc, dict) or base != "BTC":
            return None
        premium = doc.get("premium_pct")
        try:
            return float(premium) if premium is not None else None
        except (TypeError, ValueError):
            return None
