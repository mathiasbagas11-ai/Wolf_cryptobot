"""Scalp detector.

Fast intraday reversals triggered by a stop-hunt. The strongest trigger is a
**liquidity sweep**: price wicks past a recent swing level then snaps back,
trapping breakout traders. Confirmed by volume spike, RSI extreme, and — when
present — a Fair Value Gap or VWAP discount/premium that anchors the entry at a
structurally important price.

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

    def __init__(self, score_threshold: int = 65, min_recovery: float = 50.0) -> None:
        self.score_threshold = score_threshold
        self.min_recovery = min_recovery

    def evaluate(
        self, symbol: str, candles: Sequence[Candle], context=None, features=None
    ) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None

        if features is not None and features.valid:
            price = features.price
            atr = features.atr
            rsi = features.rsi
            vr = features.vol_ratio
        else:
            closes = ind.closes(candles)
            price = closes[-1]
            atr = ind.atr(candles, 14)
            rsi = ind.rsi(closes, 14)
            if any(math.isnan(x) for x in (atr, rsi)) or atr <= 0:
                return None
            vr = ind.volume_ratio(candles, 20)

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
        if not math.isnan(vr) and vr >= 2.0:
            score += 25
            reasons.append(f"Volume spike {vr:.1f}x on sweep")
        elif not math.isnan(vr) and vr >= 1.5:
            score += 12
            reasons.append(f"Volume {vr:.1f}x average")

        # 3. RSI extreme recovering in the trade direction (tightened: 35/65)
        if is_long and rsi <= 35:
            score += 25
            reasons.append(f"RSI deeply oversold: {rsi:.0f}")
        elif is_long and rsi <= 40:
            score += 10
            reasons.append(f"RSI oversold: {rsi:.0f}")
        elif not is_long and rsi >= 65:
            score += 25
            reasons.append(f"RSI deeply overbought: {rsi:.0f}")
        elif not is_long and rsi >= 60:
            score += 10
            reasons.append(f"RSI overbought: {rsi:.0f}")

        # 4. FvG confluence — sweep reached into an imbalance zone (+15)
        fvgs = ind.find_fvgs(candles, lookback=40)
        fvg_kind = "BULL" if is_long else "BEAR"
        if ind.price_in_fvg(sweep.level, fvgs, fvg_kind):
            score += 15
            reasons.append(f"Sweep into {fvg_kind} FvG — imbalance reclaimed")

        # 5. VWAP: sweep reached below/above fair value (+10)
        vwap_val = ind.vwap(candles, lookback=40)
        if not math.isnan(vwap_val):
            if is_long and sweep.level <= vwap_val:
                score += 10
                reasons.append(f"Sweep below VWAP {vwap_val:.6g} — discount entry")
            elif not is_long and sweep.level >= vwap_val:
                score += 10
                reasons.append(f"Sweep above VWAP {vwap_val:.6g} — premium entry")

        # 6. Order Block: sweep targeted a smart-money institutional zone (+10)
        obs = struct.find_order_blocks(candles, lookback=40)
        ob_kind = "BULL" if is_long else "BEAR"
        if struct.price_in_ob(sweep.level, obs, ob_kind):
            score += 10
            reasons.append(f"Sweep into {ob_kind} OB — smart-money zone hunted")

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
