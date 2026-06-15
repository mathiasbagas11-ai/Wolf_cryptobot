"""Liquidity-trap reversal detector — the anti-"exit liquidity" setup.

Retail gets trapped when price spikes through an obvious level — the prior swing
high/low where stop-losses and breakout orders pile up — only to snap straight
back inside the range. The breakout chasers who bought that move *become* the
exit liquidity smart money sells into. This detector waits for the trap to
spring and trades the **reversal**, putting you on the smart-money side instead
of being the bag-holder at the top of a fake breakout.

It is deliberately strict — threshold 80, HIGH conviction only — because a real
trap needs several tells to line up at once:

* **Liquidity sweep + reclaim** — the last candle pierces the prior 20-candle
  extreme then closes back inside (stops hunted).        (≤30 pts · hard gate)
* **Volume climax** — a blow-off volume spike on the sweep: the crowd piling
  into the fakeout.                                                  (≤30 pts)
* **Momentum exhaustion** — RSI divergence and/or an RSI extreme at the wick. (≤35)
* **Away from value** — the sweep pierced the far side of VWAP, i.e. the grab
  reached into thin air away from fair value, with room to revert.   (15 pts)
* **Rejection wick** — a dominant wick on the trap candle (absorption).(10 pts)

Geometry is mean-reversion: a tight stop just beyond the swept wick (if price
takes it out again the trap thesis is dead) and R-multiple targets back toward
value. Distinct from :class:`~wolf.detectors.scalp.ScalpDetector`, which fires on
looser sweeps with a shorter horizon — TRAP is the rarer, higher-conviction cut.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.detectors.base import Detector, SignalCandidate
from wolf.models import Candle


class LiquidityTrapDetector(Detector):
    name = "TRAP"
    min_candles = 60

    def __init__(
        self,
        score_threshold: int = 80,
        min_recovery: float = 60.0,
        max_risk_pct: float = 6.0,
    ) -> None:
        self.score_threshold = score_threshold
        # Minimum % of the sweep candle reclaimed by its close — a weak reclaim
        # is just a wick, not a sprung trap.
        self.min_recovery = min_recovery
        # Reject setups whose stop (beyond the swept wick) sits further than this
        # from entry: a very deep sweep is too wide to be a clean reversal.
        self.max_risk_pct = max_risk_pct

    def evaluate(self, symbol: str, candles: Sequence[Candle], context=None) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None
        closes = ind.closes(candles)
        price = closes[-1]
        atr = ind.atr(candles, 14)
        rsi = ind.rsi(closes, 14)
        if any(math.isnan(x) for x in (atr, rsi)) or atr <= 0:
            return None

        # Hard gate: a real, strongly-reclaimed sweep of the recent extreme.
        sweep = struct.liquidity_sweep(candles, lookback=20)
        if not sweep.swept or sweep.recovery < self.min_recovery:
            return None

        is_long = sweep.sweep_type == "BULLISH_SWEEP"  # swept lows -> reverse up
        direction = "LONG" if is_long else "SHORT"
        last = candles[-1]

        score = 0
        reasons: list[str] = []

        # 1. Sweep + reclaim (primary)
        score += 30 if sweep.recovery >= 75 else 22
        side = "lows" if is_long else "highs"
        reasons.append(f"Liquidity sweep of {side} — {sweep.recovery:.0f}% reclaim off {sweep.level:.6g}")

        # 2. Volume climax — the trapped breakout flow
        vr = ind.volume_ratio(candles, 20)
        if not math.isnan(vr):
            if vr >= 3.0:
                score += 30
                reasons.append(f"Volume blow-off {vr:.1f}x — breakout chasers trapped")
            elif vr >= 2.0:
                score += 22
                reasons.append(f"Volume climax {vr:.1f}x on the sweep")
            elif vr >= 1.5:
                score += 10
                reasons.append(f"Volume {vr:.1f}x average")

        # 3. Momentum exhaustion — divergence and/or RSI extreme at the wick
        div = struct.rsi_divergence(candles, lookback=25)
        if (is_long and div.bull_score >= 10) or (not is_long and div.bear_score >= 10):
            score += 20
            reasons.append("RSI divergence — momentum exhausted at the extreme")
        if is_long and rsi <= 35:
            score += 15
            reasons.append(f"RSI oversold {rsi:.0f} at the sweep")
        elif not is_long and rsi >= 65:
            score += 15
            reasons.append(f"RSI overbought {rsi:.0f} at the sweep")
        elif (is_long and rsi < 45) or (not is_long and rsi > 55):
            score += 7

        # 4. Away from value — the grab pierced the far side of VWAP
        vw = ind.vwap(candles)
        if not math.isnan(vw):
            if is_long and last.low < vw:
                score += 15
                reasons.append("Swept below VWAP — grab into discount, reverting to value")
            elif not is_long and last.high > vw:
                score += 15
                reasons.append("Swept above VWAP — grab into premium, reverting to value")

        # 5. Rejection wick on the trap candle (absorption)
        rng = last.high - last.low
        if rng > 0:
            lower_wick = min(last.open, last.close) - last.low
            upper_wick = last.high - max(last.open, last.close)
            dominant = lower_wick if is_long else upper_wick
            if dominant / rng >= 0.5:
                score += 10
                reasons.append("Dominant rejection wick — absorbed the sweep")

        if score < self.score_threshold:
            return None

        # Mean-reversion geometry: stop just beyond the swept wick; if price
        # reclaims that extreme the trap thesis is invalidated.
        if is_long:
            sl = last.low - atr * 0.25
            risk = price - sl
        else:
            sl = last.high + atr * 0.25
            risk = sl - price
        if risk <= 0 or (risk / price) * 100 > self.max_risk_pct:
            return None

        if is_long:
            ladder = [
                {"level": 1, "price": price + risk * 1.5},
                {"level": 2, "price": price + risk * 2.5},
            ]
        else:
            ladder = [
                {"level": 1, "price": price - risk * 1.5},
                {"level": 2, "price": price - risk * 2.5},
            ]

        return SignalCandidate(
            symbol=symbol,
            signal_type="TRAP",
            direction=direction,
            entry_price=price,
            tp=ladder[-1]["price"],
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH",  # TRAP only ever fires at high conviction
            entry_mode="MOMENTUM_NOW",
            tps=ladder,
        )
