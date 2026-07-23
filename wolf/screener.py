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
from wolf.regime_composite import MarketContext
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
        learning=None,
        macro_provider=None,
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
        self._macro_provider = macro_provider
        self._account = account
        self._risk = risk or RiskSettings()
        self._universe_provider = universe_provider
        self._min_rr = min_rr
        self._learning = learning

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
            candidate.reasons.insert(0, adj.reason)
        return True

    # ── risk gates ──────────────────────────────────────────────────────────
    # Drawdown is always a hard pause; regime + auto-pause default to MONITOR
    # (flag + down-score, still emit) and become hard blocks via RiskSettings.
    def _current_regime(self) -> str:
        if not self._risk.regime_filter_enabled or self._regime_provider is None:
            return UNKNOWN
        return self._regime_provider.bias()

    def _current_context(self) -> MarketContext:
        """Resolve the macro backdrop once per cycle.

        Prefers the composite provider (trend + flow dims); falls back to a
        trend-only context when only the legacy regime provider is wired, so
        existing behaviour and tests are unchanged when no macro provider is set.
        """
        if self._macro_provider is not None:
            try:
                return self._macro_provider.snapshot()
            except Exception:  # a macro hiccup must never break the scan
                log.warning("Composite regime snapshot failed", exc_info=True)
        return MarketContext(trend=self._current_regime())

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
        """Strategies with enough graded trades whose realized edge is negative.

        Gate on expectancy (avg PnL % per trade), not win-rate: a low-win-rate
        setup with a high reward:risk can still be net profitable, while a
        high-win-rate setup with tiny wins and large losses quietly bleeds. We
        pause a strategy only when its realized edge is below the floor.
        Win-rate is a fallback for older stats that don't carry ``avg_pnl``.
        """
        try:
            by_strategy = self._tracker.stats().get("by_strategy", {})
        except Exception:
            log.exception("Stats read failed for auto-pause")
            return set()
        weak: set[str] = set()
        for name, b in by_strategy.items():
            if b.get("total", 0) < self._risk.autopause_min_trades:
                continue
            expectancy = b.get("avg_pnl")
            if expectancy is not None:
                if expectancy < self._risk.autopause_min_expectancy:
                    weak.add(name)
            elif b.get("win_rate", 100.0) < self._risk.autopause_min_win_rate:
                weak.add(name)
        return weak

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

    def _apply_bounce_guard(self, candidate: SignalCandidate, ctx: MarketContext) -> bool:
        """Risk-scale a SHORT facing bounce/squeeze risk. Returns True to drop.

        Applies to *every* SHORT, including counter-trend setups the regime
        filter exempts — the bounce risk is a distinct axis (squeeze), not a
        trend-alignment question. In ``monitor`` mode nothing changes: the
        candidate is flagged and the what-if is logged so we collect a clean
        W/L sample. In ``live`` mode the size factor is applied and a short
        below the elevated score floor is dropped.
        """
        if not self._risk.composite_regime_enabled:
            return False
        if candidate.direction != "SHORT" or not ctx.short_reversal_risk:
            return False

        would_pass = candidate.score >= self._risk.bounce_min_score
        candidate.bounce_flagged = True
        reason = self._bounce_reason(ctx)

        if self._risk.bounce_guard_mode == "live":
            candidate.risk_scale = self._risk.bounce_size_factor
            if not would_pass:
                log.info("BOUNCE-GUARD (live): dropped SHORT %s %s score=%d < %d | %s",
                         candidate.symbol, candidate.signal_type, candidate.score,
                         self._risk.bounce_min_score, reason)
                return True
            log.info("BOUNCE-GUARD (live): scaled SHORT %s %s ×%.2f | %s",
                     candidate.symbol, candidate.signal_type, candidate.risk_scale, reason)
            return False

        # monitor: observe only
        log.info("BOUNCE-GUARD (monitor): SHORT %s %s score=%d | %s — would ×%.2f, "
                 "need score≥%d (%s)",
                 candidate.symbol, candidate.signal_type, candidate.score, reason,
                 self._risk.bounce_size_factor, self._risk.bounce_min_score,
                 "PASS" if would_pass else "FILTER")
        return False

    def _active_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Snapshot open-position counts (PENDING + ACTIVE) per strategy and per
        direction. Read once per cycle so the cap is consistent across the scan.
        """
        by_strategy: dict[str, int] = {}
        by_direction: dict[str, int] = {}
        try:
            active = self._tracker.active_signals()
        except Exception:
            log.exception("Active-signals read failed for position cap")
            return by_strategy, by_direction
        for s in active:
            by_strategy[s.strategy] = by_strategy.get(s.strategy, 0) + 1
            by_direction[s.direction] = by_direction.get(s.direction, 0) + 1
        return by_strategy, by_direction

    def _capped(
        self,
        candidate: SignalCandidate,
        by_strategy: dict[str, int],
        by_direction: dict[str, int],
    ) -> bool:
        """True when emitting ``candidate`` would exceed the per-strategy or
        per-direction concurrent-position cap. A cap <= 0 disables that limit.
        """
        cap_s = self._risk.max_active_per_strategy
        held_s = by_strategy.get(candidate.strategy, 0)
        if cap_s > 0 and held_s >= cap_s:
            log.info("Capped %s — %s already has %d active (max %d)",
                     candidate.symbol, candidate.strategy, held_s, cap_s)
            return True
        cap_d = self._risk.max_active_per_direction
        held_d = by_direction.get(candidate.direction, 0)
        if cap_d > 0 and held_d >= cap_d:
            log.info("Capped %s — %s already has %d active (max %d)",
                     candidate.symbol, candidate.direction, held_d, cap_d)
            return True
        return False

    @staticmethod
    def _bounce_reason(ctx: MarketContext) -> str:
        bits = [f"sentiment={ctx.sentiment}", f"usdt_d={ctx.usdt_d}"]
        if ctx.fng_value is not None:
            bits.append(f"fng={ctx.fng_value}")
        if ctx.usdtd_change_24h is not None:
            bits.append(f"usdtd_24h={ctx.usdtd_change_24h:+.2f}%")
        return " ".join(bits)

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
        ctx = self._current_context()
        regime = ctx.trend
        weak = self._weak_strategies()
        active_by_strategy, active_by_direction = self._active_counts()
        for symbol in self.current_universe():
            candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
            if not candles:
                continue
            features = self._build_features(candles)
            context = self._build_context(symbol)
            candidate = self._best_candidate(symbol, candles, context, features)
            if not candidate:
                continue
            if not self._apply_learning(candidate):
                continue
            if self._gate_candidate(candidate, regime, weak):
                continue
            if self._capped(candidate, active_by_strategy, active_by_direction):
                continue
            if self._apply_bounce_guard(candidate, ctx):
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
                bounce_flagged=candidate.bounce_flagged,
                risk_scale=candidate.risk_scale,
            )
            if signal is None:
                continue
            # Count this fresh position toward the cap for later symbols this cycle.
            active_by_strategy[candidate.strategy] = active_by_strategy.get(candidate.strategy, 0) + 1
            active_by_direction[candidate.direction] = active_by_direction.get(candidate.direction, 0) + 1
            recorded.append(signal)
            if self._notifier is not None:
                self._notifier.announce_signal(signal)
        log.info("Scan cycle complete: %d new signal(s)", len(recorded))
        return recorded
