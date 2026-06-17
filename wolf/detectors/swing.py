"""Swing detector.

Trend-continuation entries on a pullback to a **structurally important level** —
the spirit of the old ``detect_swing_setup``, upgraded with Fair Value Gap and
VWAP awareness. The highest-quality swing entry is a pullback that lands inside
a bullish/bearish FvG (the market's own unfilled imbalance), or near the VWAP
(fair-value reference). A rejection candle with above-average volume seals it.

Uses a ``RETEST_WAIT`` entry: the signal only becomes ACTIVE once price revisits
the entry zone, matching how swing setups are actually traded.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.detectors.base import Detector, SignalCandidate, build_targets
from wolf.models import Candle


class SwingDetector(Detector):
    name = "SWING"
    min_candles = 80

    def __init__(self, score_threshold: int = 70) -> None:
        self.score_threshold = score_threshold

    def evaluate(self, symbol: str, candles: Sequence[Candle], context=None) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None
        closes = ind.closes(candles)
        price = closes[-1]
        atr = ind.atr(candles, 14)
        rsi = ind.rsi(closes, 14)
        ema20 = ind.ema(closes, 20)
        ema50 = ind.ema(closes, 50)
        if any(math.isnan(x) for x in (atr, rsi)) or atr <= 0 or not ema20 or not ema50:
            return None

        fast, slow = ema20[-1], ema50[-1]
        uptrend = fast > slow and price > slow
        downtrend = fast < slow and price < slow
        if not (uptrend or downtrend):
            return None

        is_long = uptrend
        direction = "LONG" if is_long else "SHORT"
        last = candles[-1]
        score = 0
        reasons: list[str] = []

        # 1. Trend alignment
        score += 30
        reasons.append(f"{'Up' if is_long else 'Down'}trend: EMA20 {'>' if is_long else '<'} EMA50")

        # 2. Pullback toward the fast EMA (within 1 ATR)
        near_ema = abs(price - fast) <= atr
        if near_ema:
            score += 20
            reasons.append("Pullback to EMA20 — retest zone")

        # 3. Fair Value Gap at the pullback — highest-quality structural entry
        fvgs = ind.find_fvgs(candles, lookback=60)
        fvg_kind = "BULL" if is_long else "BEAR"
        if ind.price_in_fvg(price, fvgs, fvg_kind):
            score += 20
            reasons.append(f"Pullback inside {fvg_kind} FvG — imbalance support")

        # 3b. Order Block: pullback into institutional demand/supply zone (+20)
        obs = struct.find_order_blocks(candles, lookback=50)
        ob_kind = "BULL" if is_long else "BEAR"
        if struct.price_in_ob(price, obs, ob_kind):
            score += 20
            reasons.append(f"Pullback inside {ob_kind} Order Block — institutional zone")

        # 4. VWAP as dynamic support/resistance
        vwap_val = ind.vwap(candles, lookback=50)
        if not math.isnan(vwap_val) and abs(price - vwap_val) <= atr:
            score += 15
            reasons.append(f"Price near VWAP {vwap_val:.6g} — fair-value anchor")

        # 5. Rejection candle in trend direction
        rng = last.high - last.low
        if rng > 0:
            lower_wick = min(last.open, last.close) - last.low
            upper_wick = last.high - max(last.open, last.close)
            if is_long and last.close > last.open and lower_wick / rng >= 0.4:
                score += 20
                reasons.append("Bullish rejection candle — lower-wick demand")
            elif not is_long and last.close < last.open and upper_wick / rng >= 0.4:
                score += 20
                reasons.append("Bearish rejection candle — upper-wick supply")

        # 6. Volume on the rejection candle (confirms institutional participation)
        vr = ind.volume_ratio(candles, 20)
        if not math.isnan(vr) and vr >= 1.2:
            score += 10
            reasons.append(f"Rejection volume {vr:.1f}x average")

        # 7. Not over-extended
        if (is_long and rsi < 70) or (not is_long and rsi > 30):
            score += 5
            reasons.append(f"RSI {rsi:.0f} — room to run")

        if score < self.score_threshold:
            return None

        entry = fast
        sl, tp, ladder = build_targets(entry, atr, is_long=is_long, sl_mult=2.0, tp_mults=(2.0, 4.0))
        if (is_long and not (tp > entry > sl)) or (not is_long and not (tp < entry < sl)):
            return None
        return SignalCandidate(
            symbol=symbol,
            signal_type="SWING",
            direction=direction,
            entry_price=entry,
            tp=tp,
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH" if score >= 85 else "MEDIUM",
            entry_mode="RETEST_WAIT",
            tps=ladder,
        )
