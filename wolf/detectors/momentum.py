"""Momentum breakout detector.

A deliberately simple, fully-deterministic detector used as the reference
implementation of the :class:`~wolf.detectors.base.Detector` contract. It looks
for a breakout above the recent range confirmed by RSI strength, a positive
MACD histogram and a volume expansion, and sizes TP/SL from ATR.

It is intentionally conservative and easy to reason about — richer SMC/funding
detectors from the old bot can be added as sibling modules without touching this
one or the tracker.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf.detectors.base import Detector, SignalCandidate
from wolf.models import Candle


class MomentumBreakoutDetector(Detector):
    name = "MOMENTUM"
    min_candles = 60

    def __init__(
        self,
        rsi_long: float = 55.0,
        rsi_short: float = 45.0,
        min_volume_ratio: float = 1.5,
        atr_sl_mult: float = 1.5,
        atr_tp_mults: tuple[float, ...] = (1.5, 3.0),
        score_threshold: int = 65,
    ) -> None:
        self.rsi_long = rsi_long
        self.rsi_short = rsi_short
        self.min_volume_ratio = min_volume_ratio
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mults = atr_tp_mults
        self.score_threshold = score_threshold

    def evaluate(self, symbol: str, candles: Sequence[Candle], context=None) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None

        closes = ind.closes(candles)
        price = closes[-1]
        rsi = ind.rsi(closes, 14)
        _, _, hist = ind.macd(closes)
        vol_ratio = ind.volume_ratio(candles, 20)
        atr = ind.atr(candles, 14)
        if any(math.isnan(x) for x in (rsi, hist, vol_ratio, atr)) or atr <= 0:
            return None

        # Breakout reference: highest high / lowest low of the prior 20 candles
        # (excluding the current one).
        window = candles[-21:-1]
        recent_high = max(c.high for c in window)
        recent_low = min(c.low for c in window)

        long_break = price > recent_high
        short_break = price < recent_low

        direction: Optional[str] = None
        reasons: list[str] = []
        score = 0

        if long_break and rsi >= self.rsi_long:
            direction = "LONG"
            score += 35
            reasons.append(f"Breakout > 20-candle high ({recent_high:.6g})")
            reasons.append(f"RSI {rsi:.0f} >= {self.rsi_long:.0f}")
        elif short_break and rsi <= self.rsi_short:
            direction = "SHORT"
            score += 35
            reasons.append(f"Breakdown < 20-candle low ({recent_low:.6g})")
            reasons.append(f"RSI {rsi:.0f} <= {self.rsi_short:.0f}")
        else:
            return None

        if (direction == "LONG" and hist > 0) or (direction == "SHORT" and hist < 0):
            score += 20
            reasons.append("MACD histogram confirms")
        if vol_ratio >= self.min_volume_ratio:
            score += 20
            reasons.append(f"Volume {vol_ratio:.1f}x average")
        if (direction == "LONG" and rsi < 75) or (direction == "SHORT" and rsi > 25):
            score += 10  # not yet over-extended

        # Hard gate: LONG breakouts below EMA50 are counter-trend traps.
        # SHORT breakouts above EMA50 get a bonus instead (reward alignment).
        ema50 = ind.ema(closes, 50)
        if ema50:
            if direction == "LONG" and price < ema50[-1]:
                return None
            if (direction == "LONG" and price > ema50[-1]) or (direction == "SHORT" and price < ema50[-1]):
                score += 10
                reasons.append("EMA50 trend aligned")

        if score < self.score_threshold:
            return None

        if direction == "LONG":
            sl = price - atr * self.atr_sl_mult
            tps = [{"level": i + 1, "price": price + atr * m} for i, m in enumerate(self.atr_tp_mults)]
        else:
            sl = price + atr * self.atr_sl_mult
            tps = [{"level": i + 1, "price": price - atr * m} for i, m in enumerate(self.atr_tp_mults)]

        return SignalCandidate(
            symbol=symbol,
            signal_type="SCREENER",
            direction=direction,
            entry_price=price,
            tp=tps[-1]["price"],
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH" if score >= 85 else "MEDIUM",
            entry_mode="MOMENTUM_NOW",
            tps=tps,
        )
