"""Domain models for the tracker.

These typed dataclasses replace the ad-hoc dicts the previous bot passed around
everywhere. They serialise to/from plain dicts for JSON persistence while giving
the rest of the code attribute access, defaults and validation in one place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def is_long(self) -> bool:
        return self is Direction.LONG


class SignalType(str, Enum):
    SCREENER = "SCREENER"
    PREPUMP = "PREPUMP"
    PREDUMP = "PREDUMP"
    SCALP = "SCALP"
    SWING = "SWING"
    CONFIRMED = "CONFIRMED"


class EntryMode(str, Enum):
    MOMENTUM_NOW = "MOMENTUM_NOW"   # treated as active the moment it's sent
    RETEST_WAIT = "RETEST_WAIT"     # active only once price touches the entry zone


class Status(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED_WIN = "EXPIRED_WIN"
    EXPIRED_LOSS = "EXPIRED_LOSS"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self not in (Status.PENDING, Status.ACTIVE)

    @property
    def is_win(self) -> bool:
        return self in (Status.TP_HIT, Status.EXPIRED_WIN)

    @property
    def is_loss(self) -> bool:
        return self in (Status.SL_HIT, Status.EXPIRED_LOSS)


@dataclass(frozen=True)
class Candle:
    """A single OHLC candle. ``time`` is epoch milliseconds (Binance native)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_binance(cls, row: list) -> "Candle":
        return cls(
            time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )


@dataclass
class TpRung:
    """A single take-profit rung in the ladder."""

    level: int
    price: float

    def to_dict(self) -> dict:
        return {"level": self.level, "price": self.price}

    @classmethod
    def from_dict(cls, d: dict) -> "TpRung":
        return cls(level=int(d["level"]), price=float(d["price"]))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Signal:
    """A tracked trading signal and its lifecycle state.

    Lifecycle: PENDING -> (entry touched) ACTIVE -> TP1/TP2/.. -> TP_HIT / SL_HIT
    / INVALIDATED / EXPIRED_*.
    """

    symbol: str
    signal_type: str
    direction: str
    entry_price: float
    tp: float
    sl: float
    score: int = 0
    confluence_level: str = ""
    reasons: list[str] = field(default_factory=list)
    strategy: str = "CONFIRMED"
    entry_mode: str = EntryMode.RETEST_WAIT.value
    tp_ladder: list[dict] = field(default_factory=list)
    timeout_hours: int = 24

    # Lifecycle state
    id: str = ""
    created_at: str = field(default_factory=_now_iso)
    status: str = Status.PENDING.value
    activated: bool = False
    activated_at: Optional[str] = None
    tps_hit: list[int] = field(default_factory=list)

    # Terminal-only fields
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl_pct: Optional[float] = None
    hold_hours: Optional[float] = None
    resolved_at: Optional[str] = None

    # AI debate fields (empty when AI is not configured). In monitor mode the
    # verdict is recorded but never blocks the signal; ai_vetoed flags a signal
    # the AI would have rejected, kept for later win-rate analysis.
    ai_verdict: str = ""
    ai_confidence: int = 0
    ai_rationale: str = ""
    ai_vetoed: bool = False

    # Risk-gate flags (monitor mode). against_regime: the entry fought the broad
    # market trend; weak_strategy: emitted by an underperforming strategy. Both
    # are recorded but don't block, so we can later compare their win-rates.
    against_regime: bool = False
    weak_strategy: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.symbol}_{int(time.time() * 1000)}"
        self.reasons = list(self.reasons)[:3]
        if self.entry_mode.upper() == EntryMode.MOMENTUM_NOW.value and not self.activated:
            self.activated = True
            self.activated_at = self.activated_at or self.created_at

    @property
    def is_long(self) -> bool:
        return self.direction.upper() == Direction.LONG.value

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        # Unknown keys are dropped → safe to load old state files without ai_* fields.
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
