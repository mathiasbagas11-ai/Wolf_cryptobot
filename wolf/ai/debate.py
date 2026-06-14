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
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

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


def _describe(candidate: SignalCandidate, context=None) -> str:
    lines = [
        f"Symbol: {candidate.symbol}",
        f"Strategy: {candidate.strategy} ({candidate.signal_type})",
        f"Direction: {candidate.direction}",
        f"Entry: {candidate.entry_price:.6g}  TP: {candidate.tp:.6g}  SL: {candidate.sl:.6g}",
        f"Score: {candidate.score} ({candidate.confluence_level})",
        "Reasons:",
        *[f"  - {r}" for r in candidate.reasons],
    ]
    if context is not None:
        if getattr(context, "funding_rate", None) is not None:
            lines.append(f"Funding rate: {context.funding_rate:.4f}%")
        if getattr(context, "oi_change_pct", None) is not None:
            lines.append(f"Open-interest change: {context.oi_change_pct:+.2f}%")
    return "\n".join(lines)


class DebateValidator(SignalValidator):
    """Bull/Bear/Arbiter debate, optionally split across three providers.

    Pass a single ``client`` to run every role on one model (back-compat), or
    distinct ``bull``/``bear``/``arbiter`` clients to debate across providers —
    e.g. DeepSeek (bull) vs Groq (bear), refereed by Hermes (arbiter).
    """

    def __init__(
        self,
        client: Optional[LLMClient] = None,
        *,
        bull: Optional[LLMClient] = None,
        bear: Optional[LLMClient] = None,
        arbiter: Optional[LLMClient] = None,
    ) -> None:
        fallback = client or NullLLMClient()
        self._bull = bull or fallback
        self._bear = bear or fallback
        self._arbiter = arbiter or fallback

    @property
    def _available(self) -> bool:
        return any(c.available for c in (self._bull, self._bear, self._arbiter))

    def validate(self, candidate: SignalCandidate, context=None) -> Verdict:
        if not self._available:
            return Verdict(decision=Decision.ABSTAIN)

        setup = _describe(candidate, context)
        try:
            bull = self._bull.complete(_BULL_SYSTEM, setup, max_tokens=512)
            bear = self._bear.complete(_BEAR_SYSTEM, setup, max_tokens=512)
            arbiter_prompt = (
                f"PROPOSED SETUP:\n{setup}\n\n"
                f"BULL CASE:\n{bull or '(none)'}\n\n"
                f"BEAR CASE:\n{bear or '(none)'}\n\n"
                "Decide: CONFIRM, NEUTRAL, or REJECT."
            )
            data = self._arbiter.complete_json(_ARBITER_SYSTEM, arbiter_prompt, _VERDICT_SCHEMA, max_tokens=512)
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
