"""Price-action structure helpers.

Reusable, pure functions that detect the price-structure concepts the original
bot used inside its detectors (liquidity sweeps, RSI divergence, swing points).
Keeping them here — separate from the math indicators and from any single
detector — lets multiple detectors share one tested implementation instead of
re-deriving the logic inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from wolf import indicators as ind
from wolf.models import Candle


def swing_lows(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[int]:
    """Indices of local low pivots (low <= neighbours within the window)."""
    out: list[int] = []
    for i in range(left, len(candles) - right):
        lo = candles[i].low
        if all(candles[j].low >= lo for j in range(i - left, i)) and all(
            candles[j].low >= lo for j in range(i + 1, i + right + 1)
        ):
            out.append(i)
    return out


def swing_highs(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[int]:
    """Indices of local high pivots (high >= neighbours within the window)."""
    out: list[int] = []
    for i in range(left, len(candles) - right):
        hi = candles[i].high
        if all(candles[j].high <= hi for j in range(i - left, i)) and all(
            candles[j].high <= hi for j in range(i + 1, i + right + 1)
        ):
            out.append(i)
    return out


@dataclass
class Sweep:
    swept: bool = False
    sweep_type: str = ""        # BULLISH_SWEEP | BEARISH_SWEEP
    recovery: float = 0.0       # 0-100, how strongly price reclaimed the level
    level: float = 0.0


def liquidity_sweep(candles: Sequence[Candle], lookback: int = 20) -> Sweep:
    """Detect a stop-hunt on the most recent candle.

    * **Bullish sweep** — the last candle's low pierces the prior ``lookback``
      low but it *closes back above* that low (longs' stops hunted, reversal up).
    * **Bearish sweep** — mirror: pierces the prior high, closes back below.

    ``recovery`` measures how much of the wick was reclaimed by the close.
    """
    if len(candles) < lookback + 1:
        return Sweep()
    window = candles[-lookback - 1 : -1]
    last = candles[-1]
    prior_low = min(c.low for c in window)
    prior_high = max(c.high for c in window)
    rng = last.high - last.low

    if last.low < prior_low and last.close > prior_low and rng > 0:
        recovery = (last.close - last.low) / rng * 100
        return Sweep(True, "BULLISH_SWEEP", round(recovery, 1), prior_low)
    if last.high > prior_high and last.close < prior_high and rng > 0:
        recovery = (last.high - last.close) / rng * 100
        return Sweep(True, "BEARISH_SWEEP", round(recovery, 1), prior_high)
    return Sweep()


@dataclass
class Divergence:
    bull_score: int = 0  # regular bullish divergence: price lower-low, RSI higher-low
    bear_score: int = 0  # regular bearish divergence: price higher-high, RSI lower-high


def rsi_divergence(candles: Sequence[Candle], lookback: int = 25, period: int = 14) -> Divergence:
    """Detect regular RSI divergence over the last ``lookback`` candles."""
    if len(candles) < lookback + period:
        return Divergence()
    window = candles[-lookback:]
    closes = ind.closes(window)
    rsi = ind.rsi_series(closes, period)

    lows = swing_lows(window)
    highs = swing_highs(window)
    out = Divergence()

    # Bullish: two most recent swing lows -> price lower, RSI higher.
    valid_lows = [i for i in lows if rsi[i] == rsi[i]]  # drop NaN
    if len(valid_lows) >= 2:
        a, b = valid_lows[-2], valid_lows[-1]
        if window[b].low < window[a].low and rsi[b] > rsi[a]:
            out.bull_score = 10

    valid_highs = [i for i in highs if rsi[i] == rsi[i]]
    if len(valid_highs) >= 2:
        a, b = valid_highs[-2], valid_highs[-1]
        if window[b].high > window[a].high and rsi[b] < rsi[a]:
            out.bear_score = 10
    return out
