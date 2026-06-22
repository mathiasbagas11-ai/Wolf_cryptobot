"""Market-regime classification.

A light, candle-only read of *what kind of market* a symbol is in right now, so
the screener can treat trend-following and counter-trend setups differently. The
regime is derived from ADX (trend strength) plus the directional indicators and
EMA alignment:

* **BULLISH_TREND** — ADX above the trend floor and +DI over -DI (and price over
  its slow EMA): longs are with the trend, shorts are counter-trend.
* **BEARISH_TREND** — the mirror image.
* **RANGING** — ADX below the floor: no dominant trend, both directions are fair
  game.

The screener never *blocks* counter-trend setups outright; it only requires them
to clear a higher score bar (see :class:`~wolf.screener.Screener`). This keeps a
genuinely high-confluence reversal while filtering the marginal ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from wolf import indicators as ind
from wolf.models import Candle

BULLISH_TREND = "BULLISH_TREND"
BEARISH_TREND = "BEARISH_TREND"
RANGING = "RANGING"


@dataclass(frozen=True)
class Regime:
    label: str = RANGING
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0

    @property
    def is_trending(self) -> bool:
        return self.label in (BULLISH_TREND, BEARISH_TREND)

    def aligns_with(self, direction: str) -> bool:
        """True if ``direction`` (LONG/SHORT) is *with* the trend (or ranging)."""
        if self.label == RANGING:
            return True
        long = direction.upper() == "LONG"
        return (long and self.label == BULLISH_TREND) or (not long and self.label == BEARISH_TREND)


def detect_regime(candles: Sequence[Candle], adx_period: int = 14, adx_trend_min: float = 20.0) -> Regime:
    """Classify the current regime from candles. Falls back to RANGING on no data."""
    adx_val, plus_di, minus_di = ind.adx(candles, adx_period)
    if math.isnan(adx_val):
        return Regime()
    if adx_val < adx_trend_min:
        return Regime(RANGING, adx_val, plus_di, minus_di)
    label = BULLISH_TREND if plus_di >= minus_di else BEARISH_TREND
    return Regime(label, adx_val, plus_di, minus_di)
