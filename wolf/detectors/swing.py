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

    def __init__(self, score_threshold: int = 80) -> None:
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
            fast = features.ema20_last
            slow = features.ema50_last
            if any(math.isnan(x) for x in (fast, slow)):
                return None
        else:
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

        # Hard gate: EMA50 must be genuinely sloping in the trade direction.
        # EMA20 > EMA50 can happen in a dead-cat bounce where EMA50 is still
        # falling — the slope check filters those out.
        closes_seq = ind.closes(candles)
        ema50_series = ind.ema(closes_seq, 50)
        if not ema50_series or len(ema50_series) < 15:
            return None
        ema50_rising = ema50_series[-1] > ema50_series[-10]
        if is_long and not ema50_rising:
            return None
        if not is_long and ema50_rising:
            return None

        # Hard gate: pullback must actually reach EMA20 (within 0.7 ATR)
        if abs(price - fast) > atr * 0.7:
            return None

        # Hard gate: RSI must show genuine pullback compression
        if is_long and not (35 <= rsi <= 65):
            return None
        if not is_long and not (35 <= rsi <= 65):
            return None

        # Hard gate: require a directional rejection candle at the EMA20 level.
        # Without actual price rejection here, this is just a falling knife entry.
        rng = last.high - last.low
        if rng <= 0:
            return None
        lower_wick = min(last.open, last.close) - last.low
        upper_wick = last.high - max(last.open, last.close)
        has_rejection = (
            (is_long and last.close > last.open and lower_wick / rng >= 0.45) or
            (not is_long and last.close < last.open and upper_wick / rng >= 0.45)
        )
        if not has_rejection:
            return None

        score = 0
        reasons: list[str] = []

        # 1. Trend alignment (EMA slope already confirmed above)
        score += 30
        reasons.append(f"{'Up' if is_long else 'Down'}trend: EMA20 {'>' if is_long else '<'} EMA50 (slope confirmed)")

        # 2. Pullback to EMA20 (hard gate above; rewards precision)
        score += 20
        reasons.append("Pullback to EMA20 — retest zone")

        # 3. Rejection candle confirmed (mandatory above); reward strong wicks
        if (is_long and lower_wick / rng >= 0.60) or (not is_long and upper_wick / rng >= 0.60):
            score += 15
            reasons.append("Strong rejection candle — deep wick demand/supply")
        else:
            score += 5
            reasons.append("Rejection candle — wick demand/supply")

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

        # 6. Volume on the rejection candle (confirms institutional participation)
        vr = ind.volume_ratio(candles, 20)
        if not math.isnan(vr) and vr >= 1.3:
            score += 15
            reasons.append(f"Rejection volume {vr:.1f}x average")

        # 7. RSI compression — already gated 35-65 above; reward the ideal band
        if (is_long and 40 <= rsi <= 55) or (not is_long and 45 <= rsi <= 60):
            score += 10
            reasons.append(f"RSI {rsi:.0f} — pullback compression ideal zone")

        if score < self.score_threshold:
            return None

        entry = fast
        sl, tp, ladder = build_targets(entry, atr, is_long=is_long, sl_mult=1.5, tp_mults=(2.5, 4.0))
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
