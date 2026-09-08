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
from wolf.config import LadderSettings
from wolf.detectors.base import (
    DEFAULT_LADDER,
    Detector,
    SignalCandidate,
    ladder_from_risk,
    score_flow,
)
from wolf.models import Candle


class MomentumBreakoutDetector(Detector):
    name = "MOMENTUM"
    timeframe = "1h"
    min_candles = 60

    #: The breakout itself and the MACD confirmation are gated above, so they
    #: arrive on every signal — 55 of the threshold before anything varies.
    #: These are the components that actually distinguish one breakout from
    #: the next.
    primary_components = ("volume_surge", "structure_break", "fvg_launch")

    #: Deliberately left at the screener's default. A breakout arguably deserves
    #: a wider chase — the move it exists to catch begins at the quote — but
    #: MOMENTUM is most of the current sample, and widening it re-priced those
    #: trades rather than merely adding some: the stop does not move with the
    #: re-quote, so the risk unit stretches and the ladder, rebuilt at the same
    #: R multiples, demands a far larger price move for the same nominal 3R
    #: (5.0% -> 11.6% of entry at 1.5R of chase; 15% -> 35% to the last rung).
    #: Both ratio gates get *weaker* there — nominal R:R is unchanged and a
    #: bigger 1R passes the cost gate more easily — so the change is invisible
    #: to them. What the drop needs is measurement, not a wider limit picked
    #: from replayed shapes; the screener now records every one.
    max_chase_r = None

    def __init__(
        self,
        rsi_long: float = 60.0,
        rsi_short: float = 40.0,
        min_volume_ratio: float = 2.0,
        atr_sl_mult: float = 1.5,
        score_threshold: int = 80,
        breakout_lookback: int = 50,
        ladder: LadderSettings = DEFAULT_LADDER,
        flow_veto: bool = True,
    ) -> None:
        self.rsi_long = rsi_long
        self.rsi_short = rsi_short
        self.min_volume_ratio = min_volume_ratio
        self.atr_sl_mult = atr_sl_mult
        self.score_threshold = score_threshold
        self.breakout_lookback = breakout_lookback
        self.ladder = ladder
        self.flow_veto = flow_veto

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
            fast = features.ema20_last
            slow = features.ema50_last
            if any(math.isnan(x) for x in (hist, fast, slow)):
                return None
        else:
            closes = ind.closes(candles)
            price = closes[-1]
            rsi = ind.rsi(closes, 14)
            _, _, hist = ind.macd(closes)
            vol_ratio = ind.volume_ratio(candles, 20)
            atr = ind.atr(candles, 14)
            ema20 = ind.ema(closes, 20)
            ema50 = ind.ema(closes, 50)
            if any(math.isnan(x) for x in (rsi, hist, vol_ratio, atr)) or atr <= 0:
                return None
            if not ema20 or not ema50:
                return None
            fast, slow = ema20[-1], ema50[-1]

        # Hard gate: breakout must align with the EMA trend
        if not (fast > slow or fast < slow):  # degenerate case
            return None

        # Structural breakout reference: 50-candle high/low (stronger level)
        window = candles[-self.breakout_lookback - 1 : -1]
        recent_high = max(c.high for c in window)
        recent_low = min(c.low for c in window)

        long_break = price > recent_high
        short_break = price < recent_low

        direction: Optional[str] = None
        if long_break and rsi >= self.rsi_long and fast > slow:
            direction = "LONG"
        elif short_break and rsi <= self.rsi_short and fast < slow:
            direction = "SHORT"
        else:
            return None

        # Hard gates — all must pass before any scoring
        if (direction == "LONG" and hist <= 0) or (direction == "SHORT" and hist >= 0):
            return None  # MACD must confirm the breakout
        if vol_ratio < self.min_volume_ratio:
            return None  # volume expansion is non-negotiable

        # Hard gate: the breakout must be carried by the matching side of the
        # tape. The volume gate above is direction-blind — a breakdown prints
        # the same 2x expansion as a breakout — so a level being taken out on
        # aggressive selling is a trap, not a long. Scored small on purpose:
        # its job is to reject, not to push borderline setups over the bar.
        verdict = score_flow(candles, is_long=direction == "LONG", max_points=10)
        if verdict.conflict and self.flow_veto:
            return None

        reasons: list[str] = []
        score = 0
        parts: dict[str, int] = {}

        def award(name: str, points: int, reason: str = "") -> None:
            """Score a component and remember that it is what did the scoring."""
            nonlocal score
            score += points
            parts[name] = parts.get(name, 0) + points
            if reason:
                reasons.append(reason)

        if verdict.points:
            award("flow_agrees", verdict.points, verdict.reason)
        elif verdict.reason:
            reasons.append(verdict.reason)

        ref_level = recent_high if direction == "LONG" else recent_low
        award("breakout", 35,
              f"{'Break' if direction == 'LONG' else 'Break'}out {'above' if direction == 'LONG' else 'below'} {self.breakout_lookback}-candle level ({ref_level:.6g})")
        reasons.append(f"RSI {rsi:.0f} — momentum confirms")

        award("macd_confirms", 20, "MACD histogram confirms direction")

        # Volume bonus (already passed the 1.8x gate; reward higher expansion)
        if vol_ratio >= 2.5:
            award("volume_surge", 20, f"Volume surge {vol_ratio:.1f}x — breakout conviction")
        else:
            award("volume_ok", 10, f"Volume {vol_ratio:.1f}x average")

        # VWAP context: breakout should be with the fair-value bias (+20)
        # Breakouts against VWAP are not penalised here — the EMA trend gate
        # already ensures direction alignment; counter-VWAP breakouts in a
        # strong trend can still be valid, so we only reward, never punish.
        vwap_val = ind.vwap(candles, lookback=40)
        if not math.isnan(vwap_val):
            if direction == "LONG" and price > vwap_val:
                award("vwap_aligned", 20,
                      f"Breaking above VWAP {vwap_val:.6g} — momentum with fair value")
            elif direction == "SHORT" and price < vwap_val:
                award("vwap_aligned", 20,
                      f"Breaking below VWAP {vwap_val:.6g} — momentum with fair value")

        # FvG launch zone: breakout starting from inside an imbalance (+15)
        fvgs = ind.find_fvgs(candles, lookback=40)
        fvg_kind = "BULL" if direction == "LONG" else "BEAR"
        if ind.price_in_fvg(recent_low if direction == "LONG" else recent_high, fvgs, fvg_kind):
            award("fvg_launch", 15,
                  f"Breakout launching from {fvg_kind} FvG — imbalance resolved")

        # BOS/ChoCh: breakout aligns with a structural break (+15 BOS, +20 ChoCh)
        sb = struct.find_structure_break(candles, lookback=40)
        bos_match = sb is not None and (
            (direction == "LONG" and sb.direction == "BULLISH") or
            (direction == "SHORT" and sb.direction == "BEARISH")
        )
        if bos_match:
            award("structure_break", 20 if sb.kind == "CHOCH" else 15,
                  f"{'ChoCh' if sb.kind == 'CHOCH' else 'BOS'} {sb.direction} "
                  f"— structural break at {sb.broken_level:.6g}")

        # Not over-extended
        if (direction == "LONG" and rsi < 75) or (direction == "SHORT" and rsi > 25):
            # Scored silently: five points that move the total without
            # appearing among the reasons, so the card could not be reconciled
            # with the score printed beside it.
            award("not_overextended", 5, f"RSI {rsi:.0f} — not over-extended")

        if score < self.score_threshold:
            return None

        # MOMENTUM_NOW: enter at the breakout candle close — the confirmation bar.
        # Waiting for a retest to the broken level often means the breakout failed.
        entry = price
        is_long = direction == "LONG"
        sl = entry - atr * self.atr_sl_mult if is_long else entry + atr * self.atr_sl_mult
        tps = ladder_from_risk(entry, abs(atr * self.atr_sl_mult), is_long, self.ladder)
        if not tps:
            return None

        return SignalCandidate(
            symbol=symbol,
            signal_type="SCREENER",
            direction=direction,
            entry_price=entry,
            tp=tps[-1]["price"],
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH" if score >= 85 else "MEDIUM",
            timeframe=self.timeframe,
            entry_mode="MOMENTUM_NOW",
            tps=tps,
            max_chase_r=self.max_chase_r,
            score_parts=parts,
        )
