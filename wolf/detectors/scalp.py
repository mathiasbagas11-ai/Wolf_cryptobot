"""Scalp detector.

Fast intraday reversals triggered by a stop-hunt — the spirit of the old
``detect_scalp_setup``. The strongest trigger is a **liquidity sweep**: price
wicks past a recent swing level then snaps back, trapping breakout traders.
Confirmed by a volume spike and an RSI extreme that is starting to recover.

Direction follows the sweep (bullish sweep -> LONG, bearish -> SHORT). Tight
ATR geometry and a short timeout (set by the tracker for ``SCALP``).
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.detectors.base import Detector, SignalCandidate, build_targets
from wolf.models import Candle


class ScalpDetector(Detector):
    name = "SCALP"
    min_candles = 40

    def __init__(self, score_threshold: int = 65, min_recovery: float = 55.0) -> None:
        self.score_threshold = score_threshold
        self.min_recovery = min_recovery

    def evaluate(self, symbol: str, candles: Sequence[Candle], context=None) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None
        closes = ind.closes(candles)
        price = closes[-1]
        atr = ind.atr(candles, 14)
        rsi = ind.rsi(closes, 14)
        if any(math.isnan(x) for x in (atr, rsi)) or atr <= 0:
            return None

        sweep = struct.liquidity_sweep(candles, lookback=20)
        if not sweep.swept or sweep.recovery < self.min_recovery:
            return None

        is_long = sweep.sweep_type == "BULLISH_SWEEP"
        direction = "LONG" if is_long else "SHORT"

        score = 0
        reasons: list[str] = []

        # 1. Liquidity sweep (primary trigger)
        pts = 30 if sweep.recovery >= 70 else 20
        score += pts
        reasons.append(f"{sweep.sweep_type} — {sweep.recovery:.0f}% recovery off {sweep.level:.6g}")

        # 2. Volume spike on the trigger candle
        vr = ind.volume_ratio(candles, 20)
        if not math.isnan(vr) and vr >= 2.0:
            score += 25
            reasons.append(f"Volume spike {vr:.1f}x on sweep")
        elif not math.isnan(vr) and vr >= 1.5:
            score += 12
            reasons.append(f"Volume {vr:.1f}x average")

        # 3. RSI extreme recovering in the trade direction
        if is_long and rsi <= 40:
            score += 25
            reasons.append(f"RSI oversold recovery: {rsi:.0f}")
        elif not is_long and rsi >= 60:
            score += 25
            reasons.append(f"RSI overbought rejection: {rsi:.0f}")
        elif (is_long and rsi < 50) or (not is_long and rsi > 50):
            score += 10

        if score < self.score_threshold:
            return None

        sl, tp, ladder = build_targets(price, atr, is_long=is_long, sl_mult=1.0, tp_mults=(1.0, 2.0))
        return SignalCandidate(
            symbol=symbol,
            signal_type="SCALP",
            direction=direction,
            entry_price=price,
            tp=tp,
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH" if score >= 80 else "MEDIUM",
            entry_mode="MOMENTUM_NOW",
            tps=ladder,
        )
