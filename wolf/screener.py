"""Screening orchestration.

The :class:`Screener` is the thin replacement for the old 11k-line "hub". It
fetches candles for a universe of symbols, runs each detector, then puts the best
candidate per symbol through three gates before recording + announcing it:

1. **Learning** — a symbol on the blacklist is skipped; otherwise the candidate's
   score is nudged by the strategy/symbol's historical edge.
2. **Regime** — a counter-trend setup must clear a higher score bar; trend-aligned
   and ranging setups pass normally.
3. **AI debate** — the optional Bull/Bear/arbiter layer may veto a low-quality
   signal and annotates its verdict on the recorded signal.

All collaborators are injected, so the orchestration stays tiny and testable.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf.detectors.base import Detector, SignalCandidate
from wolf.exchange import BinanceClient
from wolf.notify import TelegramNotifier
from wolf.regime import detect_regime
from wolf.tracker import Tracker

log = logging.getLogger("wolf.screener")

# Liquid USDT pairs scanned each cycle. Kept as a plain constant; override via
# the constructor for tests or custom universes.
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
    "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
)


class Screener:
    def __init__(
        self,
        client: BinanceClient,
        tracker: Tracker,
        detectors: Sequence[Detector],
        notifier: Optional[TelegramNotifier] = None,
        universe: Sequence[str] = DEFAULT_UNIVERSE,
        interval: str = "15m",
        candle_limit: int = 150,
        context_provider=None,
        validator=None,
        veto_min_confidence: int = 70,
        learning=None,
        regime=None,
        min_publish_score: int = 50,
    ) -> None:
        self._client = client
        self._tracker = tracker
        self._detectors = list(detectors)
        self._notifier = notifier
        self._universe = list(universe)
        self._interval = interval
        self._candle_limit = candle_limit
        self._context_provider = context_provider
        self._validator = validator
        self._veto_min_confidence = veto_min_confidence
        self._learning = learning
        self._regime = regime  # RegimeSettings or None (disabled)
        self._min_publish_score = min_publish_score

    @property
    def detector_names(self) -> list[str]:
        return [d.name for d in self._detectors]

    @property
    def universe_size(self) -> int:
        return len(self._universe)

    def _build_context(self, symbol: str):
        if self._context_provider is None:
            return None
        try:
            return self._context_provider.build(symbol)
        except (ValueError, KeyError, TypeError):
            log.exception("Context build failed for %s", symbol)
            return None

    def _best_candidate(self, symbol: str, candles, context) -> Optional[SignalCandidate]:
        best: Optional[SignalCandidate] = None
        for detector in self._detectors:
            try:
                candidate = detector.evaluate(symbol, candles, context)
            except (ValueError, KeyError, TypeError, IndexError):
                log.exception("Detector %s crashed on %s", detector.name, symbol)
                continue
            if candidate and (best is None or candidate.score > best.score):
                best = candidate
        return best

    def scan_symbol(self, symbol: str) -> Optional[SignalCandidate]:
        """Return the highest-scoring candidate for ``symbol`` this cycle."""
        candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        if not candles:
            return None
        return self._best_candidate(symbol, candles, self._build_context(symbol))

    # ── gates ────────────────────────────────────────────────────────────
    def _apply_learning(self, candidate: SignalCandidate) -> bool:
        """Adjust score from memory; return False if the symbol is blacklisted."""
        if self._learning is None:
            return True
        adj = self._learning.adjustment(candidate.symbol, candidate.strategy)
        if adj.blacklisted:
            log.info("Learning skip %s: %s", candidate.symbol, adj.reason)
            return False
        if adj.delta:
            candidate.score = int(max(0, min(100, candidate.score + adj.delta)))
            candidate.reasons = [adj.reason] + candidate.reasons
        return True

    def _passes_regime(self, candidate: SignalCandidate, candles) -> bool:
        if self._regime is None or not self._regime.enabled:
            return True
        reg = detect_regime(candles, self._regime.adx_period, self._regime.adx_trend_min)
        if reg.aligns_with(candidate.direction):
            if reg.is_trending:
                candidate.reasons = candidate.reasons + [f"Regime {reg.label} (ADX {reg.adx:.0f}) — with trend"]
            return True
        # Counter-trend: only the strongest setups get through.
        if candidate.score >= self._regime.counter_trend_min_score:
            candidate.reasons = candidate.reasons + [
                f"Counter-trend vs {reg.label} (ADX {reg.adx:.0f}) — high confluence override"
            ]
            return True
        log.info("Regime filter %s %s vs %s (score %d<%d)",
                 candidate.symbol, candidate.direction, reg.label,
                 candidate.score, self._regime.counter_trend_min_score)
        return False

    def _apply_validator(self, candidate: SignalCandidate, context):
        """Run the AI debate gate. Returns ``(passed, verdict_or_None)``."""
        if self._validator is None:
            return True, None
        verdict = self._validator.validate(candidate, context)
        if verdict.rationale:
            candidate.reasons = [f"AI[{verdict.decision} {verdict.confidence}%]: {verdict.rationale}"] + candidate.reasons
        if verdict.is_reject and verdict.confidence >= self._veto_min_confidence:
            log.info("AI vetoed %s %s (%d%%): %s", candidate.symbol, candidate.direction, verdict.confidence, verdict.rationale)
            return False, verdict
        return True, verdict

    def run_cycle(self) -> list:
        """Scan the whole universe; record + announce any new signals."""
        recorded = []
        for symbol in self._universe:
            candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
            if not candles:
                continue
            context = self._build_context(symbol)
            candidate = self._best_candidate(symbol, candles, context)
            if not candidate:
                continue
            if not self._apply_learning(candidate):
                continue
            if candidate.score < self._min_publish_score:
                log.info("Skip %s: score %d below publish floor %d",
                         symbol, candidate.score, self._min_publish_score)
                continue
            if not self._passes_regime(candidate, candles):
                continue
            passed, verdict = self._apply_validator(candidate, context)
            if not passed:
                continue
            signal = self._tracker.record_signal(
                symbol=candidate.symbol,
                signal_type=candidate.signal_type,
                direction=candidate.direction,
                entry_price=candidate.entry_price,
                tp=candidate.tp,
                sl=candidate.sl,
                score=candidate.score,
                confluence_level=candidate.confluence_level,
                reasons=candidate.reasons,
                strategy=candidate.strategy,
                entry_mode=candidate.entry_mode,
                tps=candidate.tps,
                ai_decision=verdict.decision if verdict else "",
                ai_confidence=verdict.confidence if verdict else 0,
                ai_rationale=verdict.rationale if verdict else "",
            )
            if signal is None:
                continue
            recorded.append(signal)
            if self._notifier is not None:
                self._notifier.announce_signal(signal)
        log.info("Scan cycle complete: %d new signal(s)", len(recorded))
        return recorded
