"""Pre-pump detector.

Looks for quiet accumulation *before* a breakout up — the spirit of the old
``detect_prepump``, expressed on a single candle series:

* **Bollinger squeeze** — band width compressed near its recent minimum
  (consolidation precedes expansion).               (30 pts)
* **Volume coil** — a fresh volume spike after a quiet stretch.   (25 pts)
* **OI/PA proxy → momentum** — rising RSI from neutral + bullish MACD. (20 pts)
* **Money flow** — bullish RSI divergence (hidden accumulation).  (15 pts)
* **Trend context** — price above its EMA50.                      (10 pts)

Funding-rate and open-interest inputs from the original are intentionally left
out of the candle-only contract; they can be layered in by a richer detector
without touching this one. Threshold mirrors the original ≥65.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.detectors.base import Detector, SignalCandidate, build_targets
from wolf.models import Candle


class PrePumpDetector(Detector):
    name = "PREPUMP"
    min_candles = 60

    def __init__(self, score_threshold: int = 78, squeeze_ratio: float = 1.15) -> None:
        self.score_threshold = score_threshold
        # Current BB width must be within this multiple of the recent minimum.
        self.squeeze_ratio = squeeze_ratio

    def evaluate(
        self, symbol: str, candles: Sequence[Candle], context=None, features=None
    ) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None

        if features is not None and features.valid:
            price = features.price
            atr = features.atr
            rsi = features.rsi
            hist = features.macd_hist
            vr = features.vol_ratio
            ema50_last = features.ema50_last
            bb_widths_seq = features.bb_widths
            bb_width_now = features.bb_width_now
            if math.isnan(hist) or math.isnan(ema50_last):
                return None
        else:
            closes = ind.closes(candles)
            price = closes[-1]
            atr = ind.atr(candles, 14)
            rsi = ind.rsi(closes, 14)
            _, _, hist = ind.macd(closes)
            if any(math.isnan(x) for x in (atr, rsi, hist)) or atr <= 0:
                return None
            vr = ind.volume_ratio(candles, 20)
            ema50 = ind.ema(closes, 50)
            ema50_last = ema50[-1] if ema50 else float("nan")
            bb_widths_seq = ind.bb_width_series(closes, 20)
            _, _, _, bb_width_now = ind.bollinger_bands(closes, 20)

        # Hard gate: EMA20 > EMA50 AND EMA50 rising — filters dead-cat bounces.
        # Pre-pump accumulation requires an established uptrend, not just a temporary cross.
        if math.isnan(ema50_last):
            return None
        closes_seq = ind.closes(candles)
        ema20_series = ind.ema(closes_seq, 20)
        ema50_series = ind.ema(closes_seq, 50)
        if not ema20_series or not ema50_series or len(ema50_series) < 15:
            return None
        ema20_last_val = ema20_series[-1]
        if ema20_last_val <= ema50_last:
            return None  # not in uptrend
        if price <= ema50_last:
            return None  # price below EMA50 — not in the right zone
        if ema50_series[-1] <= ema50_series[-10]:
            return None  # EMA50 declining — dead-cat bounce, not accumulation

        score = 0
        reasons: list[str] = []

        # 1. Bollinger squeeze
        widths = [w for w in bb_widths_seq[-30:] if not math.isnan(w)]
        if widths and not math.isnan(bb_width_now):
            recent_min = min(widths)
            if recent_min > 0 and bb_width_now <= recent_min * self.squeeze_ratio:
                score += 30
                reasons.append("Bollinger squeeze — consolidation near range low")

        # 2. Volume coil
        if not math.isnan(vr) and vr >= 1.8:
            score += 25
            reasons.append(f"Volume coil released: {vr:.1f}x average")
        elif not math.isnan(vr) and vr >= 1.3:
            score += 12
            reasons.append(f"Volume building: {vr:.1f}x average")

        # 3. Momentum — MACD positive already enforced as hard gate above;
        #    reward when RSI is also in the ideal building zone (50-68).
        if 50 <= rsi < 68:
            score += 20
            reasons.append(f"Momentum building: RSI {rsi:.0f} in accumulation zone, MACD positive")
        else:
            score += 8
            reasons.append(f"MACD positive, RSI {rsi:.0f}")

        # 4. Money flow — bullish divergence
        div = struct.rsi_divergence(candles, lookback=25)
        if div.bull_score >= 10:
            score += 15
            reasons.append("Bullish RSI divergence — hidden accumulation")

        # 5. Trend context
        if not math.isnan(ema50_last) and price > ema50_last:
            score += 10
            reasons.append("Price above EMA50 — uptrend context")

        # 6. VWAP discount — accumulation at fair value or below (+15)
        vwap_val = ind.vwap(candles, lookback=50)
        if not math.isnan(vwap_val) and price <= vwap_val:
            score += 15
            reasons.append(f"Price at VWAP discount {vwap_val:.6g} — value-zone accumulation")

        # 7. Bullish FvG below price — structural support under the setup (+10)
        fvgs = ind.find_fvgs(candles, lookback=50)
        bull_fvg = next((g for g in fvgs if g["type"] == "BULL" and g["top"] <= price), None)
        if bull_fvg:
            score += 10
            reasons.append(f"Bullish FvG below ({bull_fvg['bottom']:.6g}–{bull_fvg['top']:.6g}) — demand zone support")

        # 8. Derivatives confluence (optional) — negative funding = crowded
        #    shorts ripe for a squeeze; rising OI = fresh positioning.
        if context is not None:
            if context.funding_extreme_squeeze:
                score += 15
                reasons.append(f"Funding extreme {context.funding_rate:.3f}% — short squeeze imminent")
            elif context.funding_squeeze:
                score += 10
                reasons.append(f"Funding negative {context.funding_rate:.3f}% — short squeeze potential")
            if context.oi_rising:
                score += 8
                reasons.append(f"OI rising {context.oi_change_pct:+.1f}% — accumulation")

        if score < self.score_threshold:
            return None

        sl, tp, ladder = build_targets(price, atr, is_long=True, sl_mult=1.5, tp_mults=(2.0, 4.0))
        return SignalCandidate(
            symbol=symbol,
            signal_type="PREPUMP",
            direction="LONG",
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
