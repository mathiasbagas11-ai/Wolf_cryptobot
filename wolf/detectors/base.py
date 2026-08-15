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

from wolf.config import LadderSettings
from wolf.models import Candle

__all__ = [
    "SignalCandidate",
    "Detector",
    "build_targets",
    "ladder_from_risk",
    "DEFAULT_LADDER",
]

#: Fallback geometry for detectors constructed without explicit settings
#: (tests, ad-hoc use). Production wiring passes ``Settings.ladder`` through.
DEFAULT_LADDER = LadderSettings()


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
    ladder_cfg: LadderSettings = DEFAULT_LADDER,
) -> tuple[float, float, list[dict]]:
    """Build ``(sl, final_tp, tp_ladder)`` from an ATR-derived stop.

    Convenience wrapper for detectors whose stop is a plain ATR multiple. The
    stop distance is 1R and :func:`ladder_from_risk` places the rungs.
    """
    sl = entry - atr * sl_mult if is_long else entry + atr * sl_mult
    ladder = ladder_from_risk(entry, abs(atr * sl_mult), is_long, ladder_cfg)
    if not ladder:
        return (0.0, 0.0, [])
    return sl, ladder[-1]["price"], ladder


def ladder_from_risk(
    entry: float,
    risk_per_unit: float,
    is_long: bool,
    ladder_cfg: LadderSettings = DEFAULT_LADDER,
) -> list[dict]:
    """Place the take-profit ladder given the distance from entry to the stop.

    This is the form detectors with a **structural** stop use — a level beyond
    the swept wick, or beyond the rejection candle — where the stop is set by
    price, not by an ATR multiple. Passing that real distance in keeps the
    ratio intact: whatever the stop costs, the last rung pays ``rr_target``
    times it.

    Each rung carries the ``allocation`` closed there, because reward:risk
    describes only the final rung. Scaling out 50% at 1R caps a winning 1:3
    trade near 1.7R, and the tracker grades on that realised figure — so the
    split belongs in the signal, not in a footnote.
    """
    if risk_per_unit <= 0 or entry <= 0:
        return []
    fractions = [f for f in ladder_cfg.tp_ladder_fractions if f > 0] or [1.0]
    allocations = ladder_cfg.allocations_for(len(fractions))
    sign = 1 if is_long else -1
    return [
        {
            "level": i,
            "price": entry + sign * risk_per_unit * ladder_cfg.rr_target * frac,
            "allocation": round(alloc, 4),
            "r_multiple": round(ladder_cfg.rr_target * frac, 3),
        }
        for i, (frac, alloc) in enumerate(zip(fractions, allocations), start=1)
    ]
