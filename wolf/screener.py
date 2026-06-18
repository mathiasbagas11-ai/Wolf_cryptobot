"""Screening orchestration.

The :class:`Screener` is the thin replacement for the old 11k-line "hub". It
fetches candles for a universe of symbols, runs each detector, records the best
candidate per symbol with the tracker, and announces it. All collaborators are
injected, so the orchestration logic itself is tiny and testable.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf.detectors.base import Detector, SignalCandidate
from wolf.exchange import BinanceClient
from wolf.notify import TelegramNotifier
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
        regime_hard_block: bool = False,
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
        self._regime_hard_block = regime_hard_block

    @property
    def detector_names(self) -> list[str]:
        return [d.name for d in self._detectors]

    @property
    def universe_size(self) -> int:
        return len(self._universe)

    def _btc_regime(self) -> str:
        """Classify BTC market regime as BULL, BEAR, or NEUTRAL using EMA alignment.

        BEAR regime blocks LONG signals when regime_hard_block is enabled —
        the single biggest source of preventable losses (11.9% WR vs 31% avg).
        """
        try:
            candles = self._client.get_klines("BTCUSDT", self._interval, self._candle_limit)
            if not candles or len(candles) < 60:
                return "NEUTRAL"
            closes = [c.close for c in candles]
            ema20 = ind.ema(closes, 20)
            ema50 = ind.ema(closes, 50)
            if not ema20 or not ema50:
                return "NEUTRAL"
            price = closes[-1]
            if ema20[-1] > ema50[-1] and price > ema50[-1]:
                return "BULL"
            if ema20[-1] < ema50[-1] and price < ema50[-1]:
                return "BEAR"
        except Exception:
            log.exception("BTC regime check failed")
        return "NEUTRAL"

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

    def _apply_validator(self, candidate: SignalCandidate, context) -> bool:
        """Run the AI debate gate. Returns False if the signal is vetoed."""
        if self._validator is None:
            return True
        verdict = self._validator.validate(candidate, context)
        if verdict.rationale:
            # Prepend so the verdict survives the Signal's top-3 reasons cap.
            candidate.reasons = [f"AI[{verdict.decision} {verdict.confidence}%]: {verdict.rationale}"] + candidate.reasons
        if verdict.is_reject and verdict.confidence >= self._veto_min_confidence:
            log.info("AI vetoed %s %s (%d%%): %s", candidate.symbol, candidate.direction, verdict.confidence, verdict.rationale)
            return False
        return True

    def run_cycle(self) -> list:
        """Scan the whole universe; record + announce any new signals."""
        # Compute BTC regime once per cycle (one extra API call, not per symbol).
        btc_regime = self._btc_regime() if self._regime_hard_block else "NEUTRAL"
        if self._regime_hard_block and btc_regime != "NEUTRAL":
            log.info("Regime: BTC %s — %s", btc_regime, "blocking LONGs" if btc_regime == "BEAR" else "all clear")

        recorded = []
        for symbol in self._universe:
            candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
            if not candles:
                continue
            context = self._build_context(symbol)
            candidate = self._best_candidate(symbol, candles, context)
            if not candidate:
                continue
            # Regime hard block: skip LONG signals when BTC is in bear regime.
            if self._regime_hard_block and candidate.direction == "LONG" and btc_regime == "BEAR":
                log.info("Regime block: %s LONG rejected (BTC BEAR)", candidate.symbol)
                continue
            if not self._apply_validator(candidate, context):
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
                btc_regime=btc_regime,
            )
            if signal is None:
                continue
            recorded.append(signal)
            if self._notifier is not None:
                self._notifier.announce_signal(signal)
        log.info("Scan cycle complete: %d new signal(s)", len(recorded))
        return recorded
