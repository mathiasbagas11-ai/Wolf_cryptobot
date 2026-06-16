"""Screening orchestration.

The :class:`Screener` is the thin replacement for the old 11k-line "hub". It
fetches candles for a universe of symbols, runs each detector, records the best
candidate per symbol with the tracker, and announces it. All collaborators are
injected, so the orchestration logic itself is tiny and testable.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf.config import RiskSettings
from wolf.detectors.base import Detector, SignalCandidate
from wolf.exchange import BinanceClient
from wolf.notify import TelegramNotifier
from wolf.regime import BEARISH, BULLISH, NEUTRAL, UNKNOWN
from wolf.tracker import Tracker

log = logging.getLogger("wolf.screener")

# Reversal setups intentionally fade the trend, so the regime filter exempts
# them — only trend-following detectors are blocked for fighting the tape.
COUNTER_TREND_TYPES: frozenset[str] = frozenset({"SCALP", "PREDUMP", "TRAP"})

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
        regime_provider=None,
        account=None,
        risk: Optional[RiskSettings] = None,
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
        self._regime_provider = regime_provider
        self._account = account
        self._risk = risk or RiskSettings()

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

    def _apply_validator(self, candidate: SignalCandidate, context) -> None:
        """Run the AI debate and annotate the candidate. Monitor mode: never blocks.

        The verdict is stored on the candidate (and later the Signal) so we can
        compare AI-flagged vs AI-confirmed signals' win-rates over time.
        """
        if self._validator is None:
            return
        verdict = self._validator.validate(candidate, context)
        candidate.ai_verdict = verdict.decision
        candidate.ai_confidence = verdict.confidence
        candidate.ai_rationale = verdict.rationale
        if verdict.is_reject and verdict.confidence >= self._veto_min_confidence:
            candidate.ai_vetoed = True
            log.info(
                "AI would veto %s %s (%d%%) — monitor mode, sending anyway: %s",
                candidate.symbol, candidate.direction, verdict.confidence, verdict.rationale,
            )
        elif verdict.rationale:
            log.info(
                "AI %s %s %s (%d%%): %s",
                verdict.decision, candidate.symbol, candidate.direction,
                verdict.confidence, verdict.rationale,
            )

    # ── risk gates (hard block) ─────────────────────────────────────────────
    def _current_regime(self) -> str:
        if not self._risk.regime_filter_enabled or self._regime_provider is None:
            return UNKNOWN
        return self._regime_provider.bias()

    def _drawdown_paused(self) -> bool:
        """True when paper equity has fallen far enough below its peak to pause."""
        if self._account is None:
            return False
        try:
            return self._account.drawdown_pct() >= self._risk.drawdown_pause_pct
        except Exception:  # equity read must never break the scan
            log.exception("Drawdown check failed")
            return False

    def _weak_strategies(self) -> set[str]:
        """Strategies with enough graded trades and a win-rate below the floor."""
        try:
            by_strategy = self._tracker.stats().get("by_strategy", {})
        except Exception:
            log.exception("Stats read failed for auto-pause")
            return set()
        return {
            name
            for name, b in by_strategy.items()
            if b.get("total", 0) >= self._risk.autopause_min_trades
            and b.get("win_rate", 100.0) < self._risk.autopause_min_win_rate
        }

    def _fights_regime(self, candidate: SignalCandidate, regime: str) -> bool:
        """True when a trend-following entry trades against the broad market."""
        if regime in (NEUTRAL, UNKNOWN):
            return False
        if candidate.signal_type in COUNTER_TREND_TYPES:
            return False  # reversal setups are meant to fade the tape
        if regime == BEARISH and candidate.direction == "LONG":
            return True
        if regime == BULLISH and candidate.direction == "SHORT":
            return True
        return False

    def _block_reason(self, candidate: SignalCandidate, regime: str, weak: set[str]) -> Optional[str]:
        """Return why a candidate is blocked, or ``None`` to let it through."""
        if candidate.strategy in weak:
            return "strategy auto-paused (underperforming)"
        if self._fights_regime(candidate, regime):
            return f"against {regime} regime"
        return None

    def run_cycle(self) -> list:
        """Scan the whole universe; record + announce any new signals."""
        recorded = []
        # Resolve cycle-wide risk state once, not per symbol.
        if self._drawdown_paused():
            log.warning(
                "Drawdown %.1f%% ≥ %.1f%% — new entries paused this cycle",
                self._account.drawdown_pct(), self._risk.drawdown_pause_pct,
            )
            return recorded
        regime = self._current_regime()
        weak = self._weak_strategies()
        for symbol in self._universe:
            candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
            if not candles:
                continue
            context = self._build_context(symbol)
            candidate = self._best_candidate(symbol, candles, context)
            if not candidate:
                continue
            blocked = self._block_reason(candidate, regime, weak)
            if blocked:
                log.info("Blocked %s %s (%s): %s", candidate.symbol, candidate.direction, candidate.strategy, blocked)
                continue
            self._apply_validator(candidate, context)
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
                ai_verdict=candidate.ai_verdict,
                ai_confidence=candidate.ai_confidence,
                ai_rationale=candidate.ai_rationale,
                ai_vetoed=candidate.ai_vetoed,
            )
            if signal is None:
                continue
            recorded.append(signal)
            if self._notifier is not None:
                self._notifier.announce_signal(signal)
        log.info("Scan cycle complete: %d new signal(s)", len(recorded))
        return recorded
