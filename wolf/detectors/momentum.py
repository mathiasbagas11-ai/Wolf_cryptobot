"""Momentum breakout detector.

Fires on a clean breakout above/below a **30-candle structural level** — a
longer lookback than the old 20-candle window, producing stronger reference
points. Three hard gates filter noise before scoring begins:
  1. MACD histogram must confirm the breakout direction.
  2. Volume must be >= 1.8x average (real momentum, not a fake-out).
  3. RSI must show conviction (>= 58 long / <= 42 short).

VWAP context and a Fair Value Gap launch zone add bonus points, rewarding
breakouts that start from a structurally significant area rather than random
mid-range price action.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.detectors.base import Detector, SignalCandidate
from wolf.models import Candle


class MomentumBreakoutDetector(Detector):
    name = "MOMENTUM"
    min_candles = 60

    def __init__(
        self,
        rsi_long: float = 58.0,
        rsi_short: float = 42.0,
        min_volume_ratio: float = 1.8,
        atr_sl_mult: float = 1.5,
        atr_tp_mults: tuple[float, ...] = (1.5, 3.0),
        score_threshold: int = 70,
        breakout_lookback: int = 30,
    ) -> None:
        self.rsi_long = rsi_long
        self.rsi_short = rsi_short
        self.min_volume_ratio = min_volume_ratio
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mults = atr_tp_mults
        self.score_threshold = score_threshold
        self.breakout_lookback = breakout_lookback

    def evaluate(
        self, symbol: str, candles: Sequence[Candle], context=None, features=None
    ) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None

        if features is not None and features.valid:
            price = features.price
            rsi = features.rsi
            hist = features.macd_hist
            vol_ratio = features.vol_ratio
            atr = features.atr
            if math.isnan(hist):
                return None
        else:
            closes = ind.closes(candles)
            price = closes[-1]
            rsi = ind.rsi(closes, 14)
            _, _, hist = ind.macd(closes)
            vol_ratio = ind.volume_ratio(candles, 20)
            atr = ind.atr(candles, 14)
            if any(math.isnan(x) for x in (rsi, hist, vol_ratio, atr)) or atr <= 0:
                return None

        # Structural breakout reference: 30-candle high/low (stronger level)
        window = candles[-self.breakout_lookback - 1 : -1]
        recent_high = max(c.high for c in window)
        recent_low = min(c.low for c in window)

        long_break = price > recent_high
        short_break = price < recent_low

        direction: Optional[str] = None
        if long_break and rsi >= self.rsi_long:
            direction = "LONG"
        elif short_break and rsi <= self.rsi_short:
            direction = "SHORT"
        else:
            return None

        # Hard gates — all three must pass before any scoring
        if (direction == "LONG" and hist <= 0) or (direction == "SHORT" and hist >= 0):
            return None  # MACD must confirm the breakout
        if vol_ratio < self.min_volume_ratio:
            return None  # volume expansion is non-negotiable

        reasons: list[str] = []
        score = 0

        ref_level = recent_high if direction == "LONG" else recent_low
        score += 35
        reasons.append(f"{'Break' if direction == 'LONG' else 'Break'}out {'above' if direction == 'LONG' else 'below'} {self.breakout_lookback}-candle level ({ref_level:.6g})")
        reasons.append(f"RSI {rsi:.0f} — momentum confirms")

        score += 20
        reasons.append("MACD histogram confirms direction")

        # Volume bonus (already passed the 1.8x gate; reward higher expansion)
        if vol_ratio >= 2.5:
            score += 20
            reasons.append(f"Volume surge {vol_ratio:.1f}x — breakout conviction")
        else:
            score += 10
            reasons.append(f"Volume {vol_ratio:.1f}x average")

        # VWAP context: breakout should be with the fair-value bias (+20/-10)
        vwap_val = ind.vwap(candles, lookback=40)
        if not math.isnan(vwap_val):
            if direction == "LONG" and price > vwap_val:
                score += 20
                reasons.append(f"Breaking above VWAP {vwap_val:.6g} — momentum with fair value")
            elif direction == "SHORT" and price < vwap_val:
                score += 20
                reasons.append(f"Breaking below VWAP {vwap_val:.6g} — momentum with fair value")
            else:
                score -= 10  # breaking against fair value — reduce confidence

        # FvG launch zone: breakout starting from inside an imbalance (+15)
        fvgs = ind.find_fvgs(candles, lookback=40)
        fvg_kind = "BULL" if direction == "LONG" else "BEAR"
        if ind.price_in_fvg(recent_low if direction == "LONG" else recent_high, fvgs, fvg_kind):
            score += 15
            reasons.append(f"Breakout launching from {fvg_kind} FvG — imbalance resolved")

        # BOS/ChoCh: breakout aligns with a structural break (+15 BOS, +20 ChoCh)
        sb = struct.find_structure_break(candles, lookback=40)
        bos_match = sb is not None and (
            (direction == "LONG" and sb.direction == "BULLISH") or
            (direction == "SHORT" and sb.direction == "BEARISH")
        )
        if bos_match:
            bonus = 20 if sb.kind == "CHOCH" else 15
            score += bonus
            reasons.append(
                f"{'ChoCh' if sb.kind == 'CHOCH' else 'BOS'} {sb.direction} "
                f"— structural break at {sb.broken_level:.6g}"
            )

        # Not over-extended
        if (direction == "LONG" and rsi < 75) or (direction == "SHORT" and rsi > 25):
            score += 5

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
