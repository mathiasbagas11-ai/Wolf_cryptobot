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


class Detector(ABC):
    """Base class for all detectors."""

    #: Human-readable strategy name (also used as the ``strategy`` tag).
    name: str = "base"

    #: Minimum number of candles required to evaluate.
    min_candles: int = 30

    @abstractmethod
    def evaluate(self, symbol: str, candles: Sequence[Candle]) -> Optional[SignalCandidate]:
        """Return a candidate if the setup triggers, else ``None``."""
        raise NotImplementedError

    def _ready(self, candles: Sequence[Candle]) -> bool:
        return len(candles) >= self.min_candles
