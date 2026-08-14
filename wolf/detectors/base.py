"""Detector contract.

Each detector is a small, self-contained unit that inspects market data for one
symbol and optionally returns a :class:`SignalCandidate`. Splitting detectors
into their own modules (instead of dozens of ``detect_*`` functions buried in an
11k-line file) is the structural fix for the old monolith: a detector can be
read, tested and changed in isolation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Sequence

from wolf.models import Candle

__all__ = ["SignalCandidate", "Detector", "build_targets"]


@dataclass
class SignalCandidate:
    """A proposed signal produced by a detector, before it is tracked."""

    symbol: str
    signal_type: str
    direction: str
    entry_price: float
    tp: float
    sl: float
    score: int
    strategy: str
    reasons: list[str] = field(default_factory=list)
    confluence_level: str = ""
    entry_mode: str = "RETEST_WAIT"
    tps: Optional[list[dict]] = None
    # Populated by Screener after the AI debate runs (monitor mode).
    ai_verdict: str = ""
    ai_confidence: int = 0
    ai_rationale: str = ""
    ai_vetoed: bool = False
    # Risk gates (monitor mode): set when the signal trades against the market
    # regime or comes from an underperforming strategy. Kept for win-rate study.
    against_regime: bool = False
    weak_strategy: bool = False
    # Composite-regime bounce guard: flagged when a SHORT faces bounce/squeeze
    # risk. ``risk_scale`` shrinks the position size (1.0 = full). In monitor
    # mode the flag is set but risk_scale stays 1.0 (observation only).
    bounce_flagged: bool = False
    risk_scale: float = 1.0


class Detector(ABC):
    """Base class for all detectors."""

    #: Human-readable strategy name (also used as the ``strategy`` tag).
    name: str = "base"

    #: Minimum number of candles required to evaluate.
    min_candles: int = 30

    @abstractmethod
    def evaluate(
        self, symbol: str, candles: Sequence[Candle], context=None, features=None
    ) -> Optional[SignalCandidate]:
        """Return a candidate if the setup triggers, else ``None``.

        ``context`` is an optional :class:`~wolf.market.MarketContext` carrying
        derivatives data (funding, OI).  ``features`` is an optional
        :class:`~wolf.indicator_cache.CandleFeatures` with pre-computed
        indicators shared across all detectors in one cycle; when present
        detectors skip redundant computation.  Both default to ``None`` so
        every detector remains fully usable with candles alone.
        """
        raise NotImplementedError

    def _ready(self, candles: Sequence[Candle]) -> bool:
        return len(candles) >= self.min_candles


def build_targets(
    entry: float,
    atr: float,
    is_long: bool,
    sl_mult: float = 1.5,
    tp_mults: tuple[float, ...] = (1.5, 3.0),
) -> tuple[float, float, list[dict]]:
    """Build ``(sl, final_tp, tp_ladder)`` from ATR.

    Shared by every detector so TP/SL sizing is consistent and defined once.
    """
    if is_long:
        sl = entry - atr * sl_mult
        ladder = [{"level": i + 1, "price": entry + atr * m} for i, m in enumerate(tp_mults)]
    else:
        sl = entry + atr * sl_mult
        ladder = [{"level": i + 1, "price": entry - atr * m} for i, m in enumerate(tp_mults)]
    return sl, ladder[-1]["price"], ladder
