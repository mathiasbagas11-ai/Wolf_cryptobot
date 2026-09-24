"""Market regime — the broad directional backdrop for signal gating.

The detectors look at one symbol at a time and have no idea whether the whole
market is trending up, rolling over, or chopping sideways. That blind spot is
what makes a breakout bot bleed: it keeps firing LONGs into a correction. This
module computes a single regime read from a bellwether (BTC by default) so the
screener can refuse trend-following entries that fight the tape.

The bias logic mirrors the existing :class:`~wolf.reports.pulse.MarketPulse`
report (price vs EMA20 vs EMA50) — same definition, now wired into the decision
to emit instead of only being narrated to Telegram.
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

from wolf import indicators as ind
from wolf.models import Candle

log = logging.getLogger("wolf.regime")

# Regime labels.
BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"
UNKNOWN = "UNKNOWN"  # not enough data / fetch failed — never used to block


def trend_bias(candles: Sequence[Candle]) -> str:
    """Classify the trend from a candle series.

    * **BULLISH** — price above EMA20 above EMA50 (stacked up).
    * **BEARISH** — price below EMA20 below EMA50 (stacked down).
    * **NEUTRAL** — EMAs interleaved / chop (no clean stack).
    * **UNKNOWN** — fewer than 60 candles or NaN math.
    """
    closes = ind.closes(candles)
    if len(closes) < 60:
        return UNKNOWN
    ema20 = ind.ema(closes, 20)
    ema50 = ind.ema(closes, 50)
    if not ema20 or not ema50:
        return UNKNOWN
    price, e20, e50 = closes[-1], ema20[-1], ema50[-1]
    if any(math.isnan(x) for x in (price, e20, e50)):
        return UNKNOWN
    if price > e20 > e50:
        return BULLISH
    if price < e20 < e50:
        return BEARISH
    return NEUTRAL


class RegimeProvider:
    """Fetches the bellwether's candles and reports the current regime."""

    def __init__(self, client, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 120) -> None:
        self._client = client
        self._symbol = symbol
        self._interval = interval
        self._limit = limit

    def bias(self) -> str:
        """Return the current regime, or ``UNKNOWN`` if it can't be determined."""
        try:
            candles = self._client.get_klines(self._symbol, self._interval, self._limit)
        except Exception:  # never let a regime fetch break the scan
            log.warning("Regime fetch failed for %s", self._symbol, exc_info=True)
            return UNKNOWN
        if not candles:
            return UNKNOWN
        return trend_bias(candles)
