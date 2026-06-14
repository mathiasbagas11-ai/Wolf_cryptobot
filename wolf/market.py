"""Market context — derivatives data used by detectors.

Captures the futures-market signals (funding rate, open-interest momentum) the
original PREPUMP/PREDUMP detectors relied on. Modelled as an immutable value
object that is *fetched once per symbol per cycle* by :class:`ContextProvider`
and passed into ``Detector.evaluate``. Keeping the data separate from the
fetching keeps detectors pure and unit-testable: a test constructs a
``MarketContext(...)`` directly with no network.

Thresholds mirror the previous bot:
* funding < -0.05%  -> short-squeeze potential (bullish for PREPUMP)
* funding < -0.10%  -> extreme short squeeze
* funding > +0.05%  -> longs overheated (bearish for PREDUMP)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("wolf.market")

FUNDING_SQUEEZE_THRESH = -0.05   # percent
FUNDING_EXTREME_THRESH = -0.10
FUNDING_OVERHEATED_THRESH = 0.05
OI_RISING_THRESH = 2.0           # percent change over the window


@dataclass(frozen=True)
class MarketContext:
    funding_rate: Optional[float] = None   # percent
    oi_change_pct: Optional[float] = None   # percent

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


class ContextProvider:
    """Builds a :class:`MarketContext` for a symbol from the exchange client."""

    def __init__(self, client) -> None:
        self._client = client

    def build(self, symbol: str) -> MarketContext:
        funding = self._client.get_funding_rate(symbol)
        oi_change = self._client.get_open_interest_change(symbol)
        return MarketContext(funding_rate=funding, oi_change_pct=oi_change)
