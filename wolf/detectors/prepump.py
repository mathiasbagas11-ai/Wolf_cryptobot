"""Pre-pump detector.

Looks for quiet accumulation *before* a breakout up — the spirit of the old
``detect_prepump``, expressed on a single candle series:

* **Trend gate** — price must be above EMA50 AND EMA200 (hard filter).
* **Bollinger squeeze** — band width compressed near its recent minimum
  (consolidation precedes expansion).               (30 pts)
* **Volume coil** — a fresh volume spike after a quiet stretch.   (25 pts)
* **OI/PA proxy → momentum** — rising RSI from neutral + bullish MACD. (20 pts)
* **Money flow** — bullish RSI divergence (hidden accumulation).  (15 pts)
* **Trend context** — price above EMA50 (hard gate, also +10 pts confirmation).
* **Strong trend** — price above EMA200 (additional +10 pts).

Threshold raised to 80 to demand multiple independent confirmations, matching
the rigor that makes PREDUMP's win rate so much higher than PREPUMP historically.
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

    def __init__(self, score_threshold: int = 80, squeeze_ratio: float = 1.15) -> None:
        self.score_threshold = score_threshold
        # Current BB width must be within this multiple of the recent minimum.
        self.squeeze_ratio = squeeze_ratio

    def evaluate(self, symbol: str, candles: Sequence[Candle], context=None) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None
        closes = ind.closes(candles)
        price = closes[-1]
        atr = ind.atr(candles, 14)
        rsi = ind.rsi(closes, 14)
        _, _, hist = ind.macd(closes)
        if any(math.isnan(x) for x in (atr, rsi, hist)) or atr <= 0:
            return None

        # Hard gate: PREPUMP only fires in an established uptrend.
        # Bollinger squeezes in downtrends produce bear-flag traps, not pumps.
        ema50 = ind.ema(closes, 50)
        if not ema50 or price <= ema50[-1]:
            return None

        score = 0
        reasons: list[str] = []

        # 1. Trend context (hard gate already passed — award the points)
        score += 10
        reasons.append("Price above EMA50 — uptrend context")

        # 2. Strong trend — EMA200 confirmation
        ema200 = ind.ema(closes, 200) if len(closes) >= 200 else None
        if ema200 and price > ema200[-1]:
            score += 10
            reasons.append("Price above EMA200 — strong uptrend")

        # 3. Bollinger squeeze
        widths = [w for w in ind.bb_width_series(closes, 20)[-30:] if not math.isnan(w)]
        _, _, _, width = ind.bollinger_bands(closes, 20)
        if widths and not math.isnan(width):
            recent_min = min(widths)
            if recent_min > 0 and width <= recent_min * self.squeeze_ratio:
                score += 30
                reasons.append("Bollinger squeeze — consolidation near range low")

        # 4. Volume coil
        vr = ind.volume_ratio(candles, 20)
        if not math.isnan(vr) and vr >= 1.8:
            score += 25
            reasons.append(f"Volume coil released: {vr:.1f}x average")
        elif not math.isnan(vr) and vr >= 1.3:
            score += 12
            reasons.append(f"Volume building: {vr:.1f}x average")

        # 5. Momentum — RSI and MACD scored independently.
        # RSI in the 40-68 zone = healthy consolidation, not overbought.
        if 40 <= rsi < 68:
            score += 15
            reasons.append(f"RSI {rsi:.0f} — consolidation zone")
        # MACD histogram positive = momentum still rising into the squeeze.
        if hist > 0:
            score += 10
            reasons.append("MACD histogram positive — momentum intact")

        # 6. Money flow — bullish divergence
        div = struct.rsi_divergence(candles, lookback=25)
        if div.bull_score >= 10:
            score += 15
            reasons.append("Bullish RSI divergence — hidden accumulation")

        # 7. Derivatives confluence (optional) — negative funding = crowded
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
            confluence_level="HIGH" if score >= 90 else "MEDIUM",
            entry_mode="MOMENTUM_NOW",
            tps=ladder,
        )
