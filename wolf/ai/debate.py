"""AI debate layer — Bull vs Bear, refereed by an arbiter.

Mirrors the previous bot's ``ai_debate`` + ``hermes_agent`` design, rebuilt
cleanly:

1. **Bull** argues *for* taking the signal.
2. **Bear** argues *against* it.
3. **Arbiter** weighs both and returns a structured :class:`Verdict`
   (CONFIRM / NEUTRAL / REJECT + confidence + rationale).

The debate is a :class:`SignalValidator`: the screener calls it on the single
best candidate per symbol (not every detector hit) to bound LLM cost. When the
underlying client is unavailable it degrades to an ``ABSTAIN`` verdict that never
blocks a signal, so the bot keeps working with the AI layer off.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence

from wolf.ai.base import LLMClient, NullLLMClient
from wolf.detectors.base import SignalCandidate

log = logging.getLogger("wolf.ai.debate")


class Decision:
    CONFIRM = "CONFIRM"
    NEUTRAL = "NEUTRAL"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"  # AI layer unavailable / errored — never blocks


@dataclass
class Verdict:
    decision: str = Decision.ABSTAIN
    confidence: int = 0  # 0-100
    rationale: str = ""
    bull_summary: str = ""
    bear_summary: str = ""

    @property
    def is_reject(self) -> bool:
        return self.decision == Decision.REJECT


class SignalValidator(ABC):
    @abstractmethod
    def validate(self, candidate: SignalCandidate, context=None) -> Verdict:
        raise NotImplementedError


_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["CONFIRM", "NEUTRAL", "REJECT"]},
        "confidence": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["decision", "confidence", "rationale"],
    "additionalProperties": False,
}

_BULL_SYSTEM = (
    "You are a disciplined crypto futures trader making the BULL case. Argue, "
    "concisely and concretely, why the proposed trade setup is likely to work. "
    "Cite the strongest supporting evidence. Be honest about weak points but "
    "advocate for taking the trade. 4 sentences max."
)
_BEAR_SYSTEM = (
    "You are a disciplined crypto futures risk manager making the BEAR case. "
    "Argue, concisely and concretely, why the proposed trade setup is likely to "
    "fail or is low-quality. Cite the strongest risks and red flags. 4 sentences max."
)
_ARBITER_SYSTEM = (
    "You are the head trader and final arbiter. Given a proposed setup and the "
    "Bull and Bear arguments, decide whether to CONFIRM, treat as NEUTRAL, or "
    "REJECT the signal. Weigh evidence quality over conviction. Return strict JSON "
    "with: decision (CONFIRM|NEUTRAL|REJECT), confidence (0-100 integer), and a "
    "one-sentence rationale. Reject setups with poor risk/reward or contradictory signals."
)


def _chart_summary(candles: Sequence, n: int = 20) -> str:
    """Build a compact OHLCV + indicator table for the last ``n`` candles.

    Gives the AI actual price data to reason about, not just the detector's
    pre-computed text labels.  Keeps the table small enough not to dominate
    the prompt (one line per candle, ~60 chars each).
    """
    from wolf import indicators as ind

    if not candles or n <= 0:
        return ""

    window = list(candles[-n:])
    closes = [c.close for c in window]
    rsi_vals = ind.rsi_series(closes, 14)

    lines = [f"=== LAST {len(window)} × 15m CANDLES (oldest → newest) ==="]
    lines.append("  bar   close     chg%    vol_ratio  rsi")

    avg_vol = sum(c.volume for c in window[:-1]) / max(len(window) - 1, 1)

    for i, c in enumerate(window):
        chg = (c.close - c.open) / c.open * 100 if c.open else 0
        vr = c.volume / avg_vol if avg_vol > 0 else 1.0
        rsi_val = rsi_vals[i]
        rsi_str = f"{rsi_val:.0f}" if not math.isnan(rsi_val) else " --"
        marker = " ← signal bar" if i == len(window) - 1 else ""
        lines.append(
            f"  [{i - len(window) + 1:3d}]  {c.close:>9.4g}  {chg:>+5.1f}%   {vr:>4.1f}x      {rsi_str:>3}{marker}"
        )

    # Summary stats
    highs = [c.high for c in window]
    lows = [c.low for c in window]
    lines.append(f"  Range: low {min(lows):.4g} — high {max(highs):.4g}")
    last_rsi = next((v for v in reversed(rsi_vals) if not math.isnan(v)), None)
    if last_rsi is not None:
        lines.append(f"  RSI(14) at signal bar: {last_rsi:.1f}")

    return "\n".join(lines)


_MTF_TIMEFRAMES = ("1d", "4h", "1h", "30m")


def _multi_tf_summary(tf_candles: dict) -> str:
    """Compact per-timeframe trend table for the AI (1D → 30M)."""
    from wolf import indicators as ind

    lines = ["=== MULTI-TIMEFRAME OVERVIEW ==="]
    for tf in _MTF_TIMEFRAMES:
        candles = tf_candles.get(tf) or []
        if not candles or len(candles) < 10:
            continue
        closes = [c.close for c in candles]
        n = len(closes)
        price = closes[-1]
        chg = (closes[-1] - closes[-2]) / closes[-2] * 100 if n >= 2 else 0.0
        ema20 = ind.ema(closes, min(20, n - 1))
        ema50 = ind.ema(closes, min(50, n - 1))
        trend = "→ NEUTRAL"
        if ema20 and ema50:
            if ema20[-1] > ema50[-1] and price > ema20[-1]:
                trend = "↑ BULL"
            elif ema20[-1] < ema50[-1] and price < ema20[-1]:
                trend = "↓ BEAR"
            elif ema20[-1] > ema50[-1]:
                trend = "↗ BULL/PULL"
            else:
                trend = "↘ BEAR/PULL"
        rsi_str = ""
        if n >= 15:
            rsi_vals = ind.rsi_series(closes[-20:], 14)
            last_rsi = next((v for v in reversed(rsi_vals) if not math.isnan(v)), None)
            if last_rsi is not None:
                rsi_str = f"  RSI {last_rsi:.0f}"
        lines.append(f"  {tf.upper():>3s}  {price:.4g}  {chg:+.2f}%  {trend}{rsi_str}")
    return "\n".join(lines)


def _describe(candidate: SignalCandidate, context=None, candles: Sequence = (), tf_candles: dict = {}) -> str:
    lines = [
        f"Symbol: {candidate.symbol}",
        f"Strategy: {candidate.strategy} ({candidate.signal_type})",
        f"Direction: {candidate.direction}",
        f"Entry: {candidate.entry_price:.6g}  TP: {candidate.tp:.6g}  SL: {candidate.sl:.6g}",
        f"R:R ratio: {abs(candidate.tp - candidate.entry_price) / max(abs(candidate.entry_price - candidate.sl), 1e-9):.2f}",
        f"Score: {candidate.score}/100 ({candidate.confluence_level})",
        "Detector reasons:",
        *[f"  - {r}" for r in candidate.reasons],
    ]
    if context is not None:
        if getattr(context, "funding_rate", None) is not None:
            lines.append(f"Funding rate: {context.funding_rate:.4f}%")
        if getattr(context, "oi_change_pct", None) is not None:
            lines.append(f"Open-interest change: {context.oi_change_pct:+.2f}%")
        if getattr(context, "btc_regime", None):
            lines.append(f"BTC market regime: {context.btc_regime}")

    if tf_candles:
        mtf = _multi_tf_summary(tf_candles)
        if mtf:
            lines.append("")
            lines.append(mtf)

    if candles:
        chart = _chart_summary(candles)
        if chart:
            lines.append("")
            lines.append(chart)

    return "\n".join(lines)


class DebateValidator(SignalValidator):
    def __init__(self, client: Optional[LLMClient] = None, chart_candles: int = 0) -> None:
        self._client = client or NullLLMClient()
        self._chart_candles = chart_candles

    def validate(self, candidate: SignalCandidate, context=None, candles: Sequence = (), tf_candles: dict = {}) -> Verdict:
        if not self._client.available:
            return Verdict(decision=Decision.ABSTAIN)

        # Use candles if chart mode is enabled and data is provided.
        chart_data = candles if (self._chart_candles > 0 and candles) else ()
        setup = _describe(candidate, context, chart_data, tf_candles=tf_candles)
        try:
            bull = self._client.complete(_BULL_SYSTEM, setup, max_tokens=512)
            bear = self._client.complete(_BEAR_SYSTEM, setup, max_tokens=512)
            arbiter_prompt = (
                f"PROPOSED SETUP:\n{setup}\n\n"
                f"BULL CASE:\n{bull or '(none)'}\n\n"
                f"BEAR CASE:\n{bear or '(none)'}\n\n"
                "Decide: CONFIRM, NEUTRAL, or REJECT."
            )
            data = self._client.complete_json(_ARBITER_SYSTEM, arbiter_prompt, _VERDICT_SCHEMA, max_tokens=512)
        except Exception:  # the AI layer must never break screening
            log.exception("Debate failed for %s — abstaining", candidate.symbol)
            return Verdict(decision=Decision.ABSTAIN)

        if not data:
            return Verdict(decision=Decision.ABSTAIN, bull_summary=bull, bear_summary=bear)

        decision = str(data.get("decision", Decision.NEUTRAL)).upper()
        if decision not in (Decision.CONFIRM, Decision.NEUTRAL, Decision.REJECT):
            decision = Decision.NEUTRAL
        try:
            confidence = max(0, min(100, int(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0
        return Verdict(
            decision=decision,
            confidence=confidence,
            rationale=str(data.get("rationale", "")),
            bull_summary=bull,
            bear_summary=bear,
        )
