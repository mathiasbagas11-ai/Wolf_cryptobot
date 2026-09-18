"""Pre-dump detector.

Looks for distribution near highs *before* a breakdown — the spirit of the old
``detect_predump`` on a single candle series, biased SHORT:

* **Bearish RSI divergence** — price prints a higher high while RSI prints a
  lower high (the strongest tell).                           (35 pts)
* **Over-extension** — RSI overbought near the recent high.    (25 pts)
* **Bearish structure** — a bearish rejection candle (upper wick) at the top. (20 pts)
* **Distribution** — volume fading on the push.                (15 pts)

Threshold ≥65, and a hard sanity gate on ATR-relative volatility.

Why the "risk/reward" award became a gate
-----------------------------------------
``atr / price < 0.1`` used to pay 5 points. Measured across 1320 bars of
randomly-shaped markets it was true on **100%** of them — ATR(14) averages
fourteen bars, so even a leg printing +19% candles only reaches 0.081. It was
not a scored component but a constant, and a constant among the awards does
nothing except shift the threshold by its own value while appearing on the card
as evidence.

It is now what it was always describing: a sanity gate. On every bar where it
used to pay, the threshold dropped by the same 5 and behaviour is unchanged. On
the rare bar where it did not — sustained ~10% ATR, which is the worst
imaginable tape to be selling a fade into — the setup is now refused outright
instead of being asked for 5 points more.

What the remaining awards are worth knowing about
-------------------------------------------------
The same sweep says how often each component lands, and the shape is worth
keeping in view: ``vwap_premium`` fires on 50.3% of bars and
``bear_fvg_above`` on 49.5%, while ``bear_divergence`` — the primary tell —
fires on 2.5%. So roughly 25 of the 65 points needed can arrive from two
near-coin-flips that say nothing about distribution, and 27% of the signals
this detector produced in the sweep carried neither a divergence nor a
rejection candle. Their modal composition reads as a *healthy uptrend*: RSI
high near a high, price above VWAP, some supply overhead, volume a little
light.

That is a hypothesis about why the strategy underperforms, not a finding, and
it is deliberately not acted on here — at ``eff`` 3 nothing on the card has
separated. What this module now does is record which components fired, so the
question becomes answerable paired: do the signals carrying primary evidence
behave differently from the ones assembled out of context? See
``primary_components`` and the ``evidence`` bucket on the diagnostic.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.config import LadderSettings
from wolf.detectors.base import (
    DEFAULT_LADDER,
    Detector,
    SignalCandidate,
    build_targets,
    score_flow,
)
from wolf.models import Candle


class PreDumpDetector(Detector):
    name = "PREDUMP"
    timeframe = "1h"
    min_candles = 60

    #: A divergence or an explicit rejection candle is evidence of
    #: distribution. Everything else this detector scores is context that
    #: merely fails to contradict it.
    primary_components = ("bear_divergence", "bearish_rejection")

    def __init__(
        self,
        score_threshold: int = 65,
        max_atr_ratio: float = 0.1,
        ladder: LadderSettings = DEFAULT_LADDER,
        flow_veto: bool = True,
    ) -> None:
        self.score_threshold = score_threshold
        # Volatility sanity gate: refuse a fade when ATR is this large a share
        # of price. Previously a 5-point award that was true on every bar
        # measured, which made it a constant dressed as evidence.
        self.max_atr_ratio = max_atr_ratio
        self.ladder = ladder
        self.flow_veto = flow_veto

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

        # Hard gate: volatility sanity. Previously 5 points at the bottom of
        # the score, true on every bar measured — see the module docstring.
        if atr / price >= self.max_atr_ratio:
            return None

        score = 0
        reasons: list[str] = []
        parts: dict[str, int] = {}

        def award(name: str, points: int, reason: str = "") -> None:
            """Score a component and remember that it is what did the scoring."""
            nonlocal score
            score += points
            parts[name] = parts.get(name, 0) + points
            if reason:
                reasons.append(reason)

        # 1. Bearish divergence (primary)
        div = struct.rsi_divergence(candles, lookback=25)
        if div.bear_score >= 10:
            award("bear_divergence", 35,
                  "Bearish RSI divergence — momentum fading at highs")

        # 2. Over-extension near recent high
        window = candles[-21:-1]
        recent_high = max(c.high for c in window)
        near_high = price >= recent_high * 0.99
        if rsi >= 70 and near_high:
            award("overbought_at_high", 25, f"Overbought RSI {rsi:.0f} near range high")
        elif rsi >= 65:
            award("rsi_elevated", 12, f"RSI elevated: {rsi:.0f}")

        # 3. Bearish rejection candle (upper wick dominates)
        last = candles[-1]
        rng = last.high - last.low
        upper_wick = last.high - max(last.open, last.close)
        if rng > 0 and upper_wick / rng >= 0.5 and last.close < last.open:
            award("bearish_rejection", 20, "Bearish rejection candle — upper-wick selling")

        # 4a. Aggressive buying into the highs vetoes the fade. "Overbought"
        #     has never been a reason to short on its own; the offer has to be
        #     in control before distribution is the right read.
        verdict = score_flow(candles, is_long=False, max_points=10)
        if verdict.conflict and self.flow_veto:
            return None
        if verdict.points:
            award("flow_agrees", verdict.points, verdict.reason)
        elif verdict.reason:
            reasons.append(verdict.reason)

        # 4b. Distribution — volume fading vs average
        if not math.isnan(vr) and vr < 0.8:
            award("volume_fading", 15, f"Volume fading: {vr:.1f}x average — distribution")

        # 5. VWAP premium — distribution at fair value or above (+15)
        vwap_val = ind.vwap(candles, lookback=50)
        if not math.isnan(vwap_val) and price >= vwap_val:
            award("vwap_premium", 15,
                  f"Price at VWAP premium {vwap_val:.6g} — distribution zone")

        # 6. Bearish FvG above price — structural resistance overhead (+10)
        fvgs = ind.find_fvgs(candles, lookback=50)
        bear_fvg = next((g for g in fvgs if g["type"] == "BEAR" and g["bottom"] >= price), None)
        if bear_fvg:
            award("bear_fvg_above", 10,
                  f"Bearish FvG above ({bear_fvg['bottom']:.6g}–{bear_fvg['top']:.6g}) — supply overhead")

        # 7. Derivatives confluence (optional) — overheated positive funding
        #    means longs are crowded and ripe for liquidation.
        if context is not None:
            if context.funding_overheated_long:
                award("funding_overheated", 15,
                      f"Funding overheated {context.funding_rate:.3f}% — longs ripe for liquidation")
            if context.oi_falling:
                award("oi_falling", 8,
                      f"OI falling {context.oi_change_pct:+.1f}% — positions unwinding")

        if score < self.score_threshold:
            return None

        sl, tp, ladder = build_targets(price, atr, is_long=False, sl_mult=1.5, ladder_cfg=self.ladder)
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
            timeframe=self.timeframe,
            entry_mode="MOMENTUM_NOW",
            tps=ladder,
            score_parts=parts,
        )
