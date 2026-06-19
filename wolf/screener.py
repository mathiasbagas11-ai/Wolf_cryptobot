"""Screening orchestration.

The :class:`Screener` is the thin replacement for the old 11k-line "hub". It
fetches candles for a universe of symbols, runs each detector, records the best
candidate per symbol with the tracker, and announces it. All collaborators are
injected, so the orchestration logic itself is tiny and testable.

Improvements over the original:
* **Shared indicator cache** — :class:`~wolf.indicator_cache.CandleFeatures` is
  built once per symbol and passed to every detector so RSI / ATR / MACD /
  volume-ratio are computed a single time instead of five.
* **Conflict detection** — when both a LONG and a SHORT detector trigger on the
  same symbol in the same cycle, the market is likely choppy; the symbol is
  skipped rather than arbitrarily picking the higher score.
* **Multi-detector confluence bonus** — when two or more detectors agree on
  direction, the best candidate earns +10 score points and is promoted to HIGH
  confluence, signalling unusually strong agreement.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf.config import RiskSettings
from wolf.detectors.base import Detector, SignalCandidate
from wolf.exchange import BinanceClient
from wolf.indicator_cache import CandleFeatures
from wolf.notify import TelegramNotifier
from wolf.regime import BEARISH, BULLISH, NEUTRAL, UNKNOWN
from wolf.tracker import Tracker

log = logging.getLogger("wolf.screener")

# Reversal setups intentionally fade the trend, so the regime filter exempts
# them — only trend-following detectors are gated for fighting the tape.
COUNTER_TREND_TYPES: frozenset[str] = frozenset({"SCALP", "PREDUMP", "TRAP"})

# Score penalties applied in monitor mode so a flagged signal reads as lower
# quality without being dropped.
REGIME_PENALTY = 15
WEAK_STRATEGY_PENALTY = 10

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
        universe_provider=None,
        min_rr: float = 1.5,
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
        self._universe_provider = universe_provider
        self._min_rr = min_rr

    @property
    def detector_names(self) -> list[str]:
        return [d.name for d in self._detectors]

    def current_universe(self) -> list[str]:
        """Resolve the symbols to scan — dynamic when a provider is set."""
        if self._universe_provider is not None:
            symbols = self._universe_provider.symbols()
            if symbols:
                return symbols
        return list(self._universe)

    @property
    def universe_size(self) -> int:
        return len(self.current_universe())

    def _build_context(self, symbol: str):
        if self._context_provider is None:
            return None
        try:
            return self._context_provider.build(symbol)
        except (ValueError, KeyError, TypeError):
            log.exception("Context build failed for %s", symbol)
            return None

    def _build_features(self, candles) -> Optional[CandleFeatures]:
        """Compute shared indicators once for the given candle set."""
        if not candles:
            return None
        try:
            return CandleFeatures.build(candles)
        except Exception:
            log.exception("Feature pre-computation failed")
            return None

    def _best_candidate(
        self, symbol: str, candles, context, features: Optional[CandleFeatures] = None
    ) -> Optional[SignalCandidate]:
        """Evaluate all detectors; apply conflict check and confluence bonus."""
        all_candidates: list[SignalCandidate] = []
        for detector in self._detectors:
            try:
                candidate = detector.evaluate(symbol, candles, context, features)
            except (ValueError, KeyError, TypeError, IndexError):
                log.exception("Detector %s crashed on %s", detector.name, symbol)
                continue
            if candidate:
                all_candidates.append(candidate)

        if not all_candidates:
            return None

        # Conflict detection: if detectors disagree on direction (both LONG and
        # SHORT passed their own quality threshold), the market is choppy or
        # transitioning — emit nothing rather than guess.
        long_triggered = any(c.direction == "LONG" for c in all_candidates)
        short_triggered = any(c.direction == "SHORT" for c in all_candidates)
        if long_triggered and short_triggered:
            log.info(
                "Signal conflict on %s (LONG vs SHORT both triggered) — skipping choppy setup",
                symbol,
            )
            return None

        # Select best by score.
        best = max(all_candidates, key=lambda c: c.score)

        # Multi-detector confluence: when ≥2 detectors agree on direction, the
        # setup is stronger than any single indicator suggests.  Add a flat
        # bonus and promote confluence_level so the operator can see it.
        agreeing = [c for c in all_candidates if c.direction == best.direction and c is not best]
        if agreeing:
            best.score = min(best.score + 10, 100)
            strats = "+".join(c.strategy for c in agreeing)
            best.reasons.insert(0, f"Confluence [{best.strategy}+{strats}]")
            best.confluence_level = "HIGH"

        return best

    def scan_symbol(self, symbol: str) -> Optional[SignalCandidate]:
        """Return the highest-scoring candidate for ``symbol`` this cycle."""
        candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        if not candles:
            return None
        features = self._build_features(candles)
        return self._best_candidate(symbol, candles, self._build_context(symbol), features)

    def _fetch_tf_candles(self, symbol: str) -> dict:
        """Fetch higher-TF candles for AI multi-timeframe context."""
        tf_candles: dict = {}
        for tf in ("1d", "4h", "1h", "30m"):
            try:
                c = self._client.get_klines(symbol, tf, 50)
                if c:
                    tf_candles[tf] = c
            except Exception:
                pass
        return tf_candles

    def _apply_validator(self, candidate: SignalCandidate, context, candles=(), tf_candles: dict = {}) -> None:
        """Run the AI debate and annotate the candidate. Monitor mode: never blocks.

        The verdict is stored on the candidate (and later the Signal) so we can
        compare AI-flagged vs AI-confirmed signals' win-rates over time.
        """
        if self._validator is None:
            return
        verdict = self._validator.validate(candidate, context, candles=candles, tf_candles=tf_candles)
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

    # ── risk gates ──────────────────────────────────────────────────────────
    # Drawdown is always a hard pause; regime + auto-pause default to MONITOR
    # (flag + down-score, still emit) and become hard blocks via RiskSettings.
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

    def _gate_candidate(self, candidate: SignalCandidate, regime: str, weak: set[str]) -> bool:
        """Apply the regime + auto-pause gates. Returns True if hard-blocked.

        In monitor mode the candidate is flagged and down-scored in place but
        still emitted; in hard mode the method signals the caller to drop it.
        """
        if self._fights_regime(candidate, regime):
            if self._risk.regime_hard_block:
                log.info("Blocked %s %s — against %s regime", candidate.symbol, candidate.direction, regime)
                return True
            candidate.against_regime = True
            candidate.score = max(0, candidate.score - REGIME_PENALTY)
            log.info("Flagged %s %s against %s regime (monitor)", candidate.symbol, candidate.direction, regime)

        if candidate.strategy in weak:
            if self._risk.autopause_hard_block:
                log.info("Blocked %s — strategy %s auto-paused", candidate.symbol, candidate.strategy)
                return True
            candidate.weak_strategy = True
            candidate.score = max(0, candidate.score - WEAK_STRATEGY_PENALTY)
            log.info("Flagged %s — strategy %s underperforming (monitor)", candidate.symbol, candidate.strategy)

        return False

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
        for symbol in self.current_universe():
            candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
            if not candles:
                continue
            features = self._build_features(candles)
            context = self._build_context(symbol)
            candidate = self._best_candidate(symbol, candles, context, features)
            if not candidate:
                continue
            if self._gate_candidate(candidate, regime, weak):
                continue
            rr = abs(candidate.tp - candidate.entry_price) / max(abs(candidate.entry_price - candidate.sl), 1e-9)
            if rr < self._min_rr:
                log.debug("Skip %s %s: R:R %.2f < %.1f", candidate.symbol, candidate.direction, rr, self._min_rr)
                continue
            tf_candles = self._fetch_tf_candles(symbol) if self._validator is not None else {}
            self._apply_validator(candidate, context, candles, tf_candles)
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
                against_regime=candidate.against_regime,
                weak_strategy=candidate.weak_strategy,
            )
            if signal is None:
                continue
            recorded.append(signal)
            if self._notifier is not None:
                self._notifier.announce_signal(signal)
        log.info("Scan cycle complete: %d new signal(s)", len(recorded))
        return recorded
