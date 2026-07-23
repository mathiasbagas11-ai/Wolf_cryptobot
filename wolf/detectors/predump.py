"""Pre-dump detector.

Looks for distribution near highs *before* a breakdown — the spirit of the old
``detect_predump`` on a single candle series, biased SHORT:

* **Bearish RSI divergence** — price prints a higher high while RSI prints a
  lower high (the strongest tell).                           (35 pts)
* **Over-extension** — RSI overbought near the recent high.    (25 pts)
* **Bearish structure** — a bearish rejection candle (upper wick) at the top. (20 pts)
* **Distribution** — volume fading on the push.                (15 pts)
* **Risk/reward** — sane ATR-based geometry.                   (5 pts)

Threshold mirrors the original ≥65.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.detectors.base import Detector, SignalCandidate, build_targets
from wolf.models import Candle


class PreDumpDetector(Detector):
    name = "PREDUMP"
    min_candles = 60

    def __init__(self, score_threshold: int = 70) -> None:
        self.score_threshold = score_threshold

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

        score = 0
        reasons: list[str] = []

        # 1. Bearish divergence (primary)
        div = struct.rsi_divergence(candles, lookback=25)
        if div.bear_score >= 10:
            score += 35
            reasons.append("Bearish RSI divergence — momentum fading at highs")

        # 2. Over-extension near recent high
        window = candles[-21:-1]
        recent_high = max(c.high for c in window)
        near_high = price >= recent_high * 0.99
        if rsi >= 70 and near_high:
            score += 25
            reasons.append(f"Overbought RSI {rsi:.0f} near range high")
        elif rsi >= 65:
            score += 12
            reasons.append(f"RSI elevated: {rsi:.0f}")

        # 3. Bearish rejection candle (upper wick dominates)
        last = candles[-1]
        rng = last.high - last.low
        upper_wick = last.high - max(last.open, last.close)
        if rng > 0 and upper_wick / rng >= 0.5 and last.close < last.open:
            score += 20
            reasons.append("Bearish rejection candle — upper-wick selling")

        # 4. Distribution — volume fading vs average
        if not math.isnan(vr) and vr < 0.8:
            score += 15
            reasons.append(f"Volume fading: {vr:.1f}x average — distribution")

        # 5. VWAP premium — distribution at fair value or above (+15)
        vwap_val = ind.vwap(candles, lookback=50)
        if not math.isnan(vwap_val) and price >= vwap_val:
            score += 15
            reasons.append(f"Price at VWAP premium {vwap_val:.6g} — distribution zone")

        # 6. Bearish FvG above price — structural resistance overhead (+10)
        fvgs = ind.find_fvgs(candles, lookback=50)
        bear_fvg = next((g for g in fvgs if g["type"] == "BEAR" and g["bottom"] >= price), None)
        if bear_fvg:
            score += 10
            reasons.append(f"Bearish FvG above ({bear_fvg['bottom']:.6g}–{bear_fvg['top']:.6g}) — supply overhead")

        # 7. Risk/reward sanity
        if atr / price < 0.1:
            score += 5

        # 8. Derivatives confluence (optional) — overheated positive funding
        #    means longs are crowded and ripe for liquidation.
        if context is not None:
            if context.funding_overheated_long:
                score += 15
                reasons.append(f"Funding overheated {context.funding_rate:.3f}% — longs ripe for liquidation")
            if context.oi_falling:
                score += 8
                reasons.append(f"OI falling {context.oi_change_pct:+.1f}% — positions unwinding")

        if score < self.score_threshold:
            return None

        sl, tp, ladder = build_targets(price, atr, is_long=False, sl_mult=1.5, tp_mults=(2.0, 4.0))
        return SignalCandidate(
            symbol=symbol,
            signal_type="PREDUMP",
            direction="SHORT",
            entry_price=price,
            tp=tp,
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH" if score >= 85 else "MEDIUM",
            entry_mode="MOMENTUM_NOW",
            tps=ladder,
        )
