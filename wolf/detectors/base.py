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

from wolf import orderflow
from wolf.config import LadderSettings
from wolf.models import Candle

__all__ = [
    "SignalCandidate",
    "Detector",
    "build_targets",
    "ladder_from_risk",
    "score_flow",
    "FlowVerdict",
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
    #: Interval the setup was read on — carried through so the signal card can
    #: say how long the trade is meant to be held.
    timeframe: str = "15m"
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
    #: True once the screener has re-quoted ``entry_price`` at the live market
    #: price. Detectors leave it False, because they price off the last closed
    #: bar of their timeframe — and the tracker has to know which of the two it
    #: is holding to know when the position went live.
    entry_quoted_live: bool = False


class Detector(ABC):
    """Base class for all detectors."""

    #: Human-readable strategy name (also used as the ``strategy`` tag).
    name: str = "base"

    #: Minimum number of candles required to evaluate.
    min_candles: int = 30

    #: Candle interval this detector reads. It sets the whole character of the
    #: signal, because every distance in the setup is derived from ATR on this
    #: series: a 15m ATR is a fraction of a percent, so a "1:3" trade off it
    #: targets well under 1% and resolves in minutes. The same logic on 4h
    #: candles produces a stop and targets an order of magnitude wider, which
    #: is what a position held for days actually needs. Timeouts follow from
    #: this too — see TrackerSettings.
    timeframe: str = "15m"

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


@dataclass(frozen=True)
class FlowVerdict:
    """Outcome of the shared order-flow gate."""

    points: int = 0
    reason: str = ""
    conflict: bool = False   # measured flow points against the trade
    state: Optional[orderflow.FlowState] = None


def score_flow(
    candles: Sequence[Candle],
    is_long: bool,
    fast: int = 4,
    baseline: int = 16,
    max_points: int = 25,
) -> FlowVerdict:
    """Score short-term order flow for a trade in ``is_long``'s direction.

    Volume expansion on its own is direction-blind: a capitulation sells as
    hard as a breakout buys, so a size-only test ("volume is 2x average, add
    points") rewards the trap and the setup identically. Points are awarded
    only when the *aggressive* side of that volume agrees with the trade, and a
    measured disagreement is reported as a ``conflict`` for the caller to
    reject on.

    Venues that publish no taker split cannot answer the direction question.
    Rather than guess, the gate falls back to price direction over the same
    window at ~40% credit and never vetoes on it — half of the test is a hint,
    not a verdict.
    """
    state = orderflow.analyse(candles, fast=fast, baseline=baseline)

    if not state.is_measurable:
        return _unverified(state, is_long, max_points)

    if state.conflicts_with(is_long):
        side = "selling" if is_long else "buying"
        return FlowVerdict(
            points=0,
            reason=f"Flow {state.label()} — aggressive {side} against the setup",
            conflict=True,
            state=state,
        )

    if not state.agrees_with(is_long):
        # MIXED: activity without a directional mandate.
        return FlowVerdict(points=0, reason="", conflict=False, state=state)

    if state.is_hot:
        points, detail = max_points, "expanding hard"
    elif state.is_expanding:
        points, detail = int(max_points * 0.6), "expanding"
    else:
        points, detail = int(max_points * 0.3), "fading but directional"

    dominant = state.buy_share if is_long else 1 - state.buy_share
    reason = (
        f"Flow {state.label()} — {detail}, "
        f"{dominant * 100:.0f}% taker {'buys' if is_long else 'sells'}"
    )
    if state.trade_ratio == state.trade_ratio and state.trade_ratio >= 1.3:
        points = min(max_points, points + 5)
        reason += f", trades {state.trade_ratio:.1f}x"
    return FlowVerdict(points=points, reason=reason, conflict=False, state=state)


def _unverified(state: orderflow.FlowState, is_long: bool, max_points: int) -> FlowVerdict:
    """Score a venue that publishes no buy/sell split.

    Falls back to price direction over the flow window, capped at ~40% of the
    full award. ``conflict`` is never set: without the aggressor data there is
    nothing solid enough to reject a setup on.
    """
    wanted = "BULLISH" if is_long else "BEARISH"
    if state.price_direction != wanted or not state.is_expanding:
        return FlowVerdict(points=0, reason="", conflict=False, state=state)
    return FlowVerdict(
        points=int(max_points * 0.4),
        reason=(
            f"Volume {state.volume_ratio:.1f}x baseline with price "
            f"({state.price_change * 100:+.1f}%) — direction unverified, no taker data"
        ),
        conflict=False,
        state=state,
    )


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
