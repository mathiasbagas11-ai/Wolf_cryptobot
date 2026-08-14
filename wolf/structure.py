"""Price-action structure helpers.

Reusable, pure functions that detect the price-structure concepts the original
bot used inside its detectors (liquidity sweeps, RSI divergence, swing points).
Keeping them here — separate from the math indicators and from any single
detector — lets multiple detectors share one tested implementation instead of
re-deriving the logic inline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

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
    bull_score: int = 0  # hidden accumulation: price lower-low, RSI higher-low
    bear_score: int = 0  # hidden distribution: price higher-high, RSI lower-high


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


@dataclass
class OrderBlock:
    kind: str        # "BULL" | "BEAR"
    top: float       # body high: max(open, close) of the OB candle
    bottom: float    # body low:  min(open, close) of the OB candle


def find_order_blocks(
    candles: Sequence[Candle],
    lookback: int = 50,
    impulse_candles: int = 3,
) -> list[OrderBlock]:
    """Identify Order Blocks — zones where institutional orders drove a displacement.

    A bullish OB is the last bearish candle before a sustained upward impulse
    (``impulse_candles`` consecutive bullish closes with total move ≥ 0.5 ATR).
    Smart money placed buy orders in this zone; it tends to act as support when
    price returns. A bearish OB mirrors this for downward impulses.

    Zone = candle body: ``[min(open, close), max(open, close)]``.
    """
    n = len(candles)
    if n < lookback + impulse_candles + 5:
        return []
    atr_val = ind.atr(candles, 14)
    if math.isnan(atr_val) or atr_val <= 0:
        return []
    min_move = atr_val * 0.5

    window = list(candles[max(0, n - lookback):])
    m = len(window)
    blocks: list[OrderBlock] = []

    for i in range(1, m - impulse_candles):
        # Bullish impulse: impulse_candles consecutive bullish closes
        if all(window[i + j].close > window[i + j].open for j in range(impulse_candles)):
            total_up = window[i + impulse_candles - 1].close - window[i].open
            ob = window[i - 1]
            if total_up >= min_move and ob.close < ob.open:  # preceding candle is bearish → OB
                blocks.append(OrderBlock(
                    "BULL",
                    top=max(ob.open, ob.close),
                    bottom=min(ob.open, ob.close),
                ))

        # Bearish impulse: impulse_candles consecutive bearish closes
        if all(window[i + j].close < window[i + j].open for j in range(impulse_candles)):
            total_down = window[i].open - window[i + impulse_candles - 1].close
            ob = window[i - 1]
            if total_down >= min_move and ob.close > ob.open:  # preceding candle is bullish → OB
                blocks.append(OrderBlock(
                    "BEAR",
                    top=max(ob.open, ob.close),
                    bottom=min(ob.open, ob.close),
                ))

    return blocks


def price_in_ob(price: float, blocks: list[OrderBlock], kind: str) -> bool:
    """True when ``price`` sits inside any Order Block of the requested kind."""
    return any(b.kind == kind and b.bottom <= price <= b.top for b in blocks)


@dataclass
class StructureBreak:
    kind: str        # "BOS" (trend continuation) | "CHOCH" (reversal signal)
    direction: str   # "BULLISH" | "BEARISH"
    broken_level: float


def find_structure_break(
    candles: Sequence[Candle],
    lookback: int = 40,
) -> Optional[StructureBreak]:
    """Detect a Break of Structure or Change of Character on the latest candle.

    BOS (Break of Structure): price closes above/below a confirmed swing pivot,
    extending the current trend. ChoCh (Change of Character): same break but the
    prior sequence was making lower highs (or higher lows) — a structural reversal.

    Only the most recent candle is evaluated; ``lookback`` determines how far back
    to search for confirmed swing pivots.
    """
    if len(candles) < lookback:
        return None

    window = list(candles[-lookback:])
    last = window[-1]

    # Confirmed pivots only: need 2 neighbours on each side, and must not be
    # within the last 3 candles (right-side confirmation not yet available).
    highs_idx = [i for i in swing_highs(window, left=2, right=2) if i < len(window) - 3]
    lows_idx  = [i for i in swing_lows(window, left=2, right=2) if i < len(window) - 3]

    # Bullish BOS/ChoCh: close above the most recent confirmed swing high
    if highs_idx:
        sh_price = window[highs_idx[-1]].high
        if last.close > sh_price:
            prior_highs = [window[h].high for h in highs_idx[:-1][-2:]]
            choch = len(prior_highs) >= 2 and prior_highs[-1] < prior_highs[-2]
            return StructureBreak("CHOCH" if choch else "BOS", "BULLISH", sh_price)

    # Bearish BOS/ChoCh: close below the most recent confirmed swing low
    if lows_idx:
        sl_price = window[lows_idx[-1]].low
        if last.close < sl_price:
            prior_lows = [window[l].low for l in lows_idx[:-1][-2:]]
            choch = len(prior_lows) >= 2 and prior_lows[-1] > prior_lows[-2]
            return StructureBreak("CHOCH" if choch else "BOS", "BEARISH", sl_price)

    return None
