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
    NEWS = "NEWS"


# Bar length of every interval the bot asks the exchange for. Lives here
# because both the screener (dropping the bar still forming) and the tracker
# (finding the bar an entry price was printed on) need it, and the tracker
# cannot import the screener.
INTERVAL_MS: dict[str, int] = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
}


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
    EXPIRED_FLAT = "EXPIRED_FLAT"  # timed out inside the noise band — no verdict
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

    @property
    def is_graded(self) -> bool:
        """Whether this outcome carries a win/loss verdict at all.

        EXPIRED_FLAT deliberately does not: an exit inside the noise band says
        nothing about the setup, and scoring it either way is how a short
        timeout manufactures a win rate out of coin flips.
        """
        return self.is_win or self.is_loss


@dataclass(frozen=True)
class Candle:
    """A single OHLC candle. ``time`` is epoch milliseconds (Binance native).

    ``trades`` and ``taker_buy_volume`` are optional microstructure fields that
    make order-flow analysis possible (see :mod:`wolf.orderflow`):

    * ``trades`` — executions in the bar. Trade **count** acceleration is a
      different signal from **volume** acceleration: many tiny fills inflate
      one without the other, which is what separates real participation from
      bot churn.
    * ``taker_buy_volume`` — the share of ``volume`` lifted by aggressive
      buyers; ``volume`` minus it is aggressive selling. This is what makes a
      breakout distinguishable from a breakdown, since both print big volume.

    Both default to ``0.0``. Only Binance publishes them, so candles from other
    venues simply carry no flow reading and the analysis degrades to neutral
    rather than inventing one.
    """

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    trades: int = 0
    taker_buy_volume: float = 0.0

    @classmethod
    def from_binance(cls, row: list) -> "Candle":
        # Binance kline row: [openTime, o, h, l, c, volume, closeTime,
        # quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]
        return cls(
            time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
            trades=int(row[8]) if len(row) > 8 else 0,
            taker_buy_volume=float(row[9]) if len(row) > 9 else 0.0,
        )

    @property
    def taker_sell_volume(self) -> float:
        """Aggressive sell volume — the complement of ``taker_buy_volume``."""
        return max(self.volume - self.taker_buy_volume, 0.0)

    @property
    def has_flow_data(self) -> bool:
        """True when this candle carries a usable buy/sell split."""
        return self.volume > 0 and self.taker_buy_volume > 0


@dataclass
class TpRung:
    """A single take-profit rung in the ladder.

    ``allocation`` is the fraction of the position closed here. Without it a
    ladder is only a list of prices, and a scaled exit has to be priced by
    assuming every rung carries equal size — which overstates the far rungs
    that rarely fill. ``0.0`` means "not specified" and callers fall back to an
    even split, so ladders stored before this field still grade.
    """

    level: int
    price: float
    allocation: float = 0.0
    r_multiple: float = 0.0

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "price": self.price,
            "allocation": self.allocation,
            "r_multiple": self.r_multiple,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TpRung":
        return cls(
            level=int(d["level"]),
            price=float(d["price"]),
            allocation=float(d.get("allocation") or 0.0),
            r_multiple=float(d.get("r_multiple") or 0.0),
        )


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
    #: Candle interval the setup was read on. Determines how wide the targets
    #: are and how long the trade is meant to be held.
    timeframe: str = "15m"
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
    # PnL in units of the trade's own risk (R = pnl_pct / distance to SL).
    # Targets are ATR multiples, so raw percentages are not comparable across
    # symbols: -1 ATR is -0.3% on a quiet coin and -3% on a volatile one, and
    # averaging those in % lets the volatile handful dominate the report.
    r_multiple: Optional[float] = None
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
    # Composite-regime bounce guard. bounce_flagged: a SHORT emitted into
    # bounce/squeeze risk (recorded even in monitor mode for the what-if study).
    # risk_scale: position-size multiplier actually applied (1.0 = full size).
    bounce_flagged: bool = False
    risk_scale: float = 1.0
    # entry_quoted_live: the entry was taken from the live price feed at
    # created_at, rather than from the last closed bar of the timeframe.
    entry_quoted_live: bool = False

    # Top-of-book spread at signal time, in basis points of the mid.
    #
    # The cost of a round trip is two fees plus one full spread — a taker buys
    # the ask and sells the bid — and only the fees are the same on every
    # symbol. BTC quotes well under a basis point while a coin that has just
    # rotated into the volume-ranked universe can quote ten, so charging the
    # whole book one configured constant models a 10-20x range as a point.
    #
    # Recorded at signal time because that is the only moment it is knowable:
    # read off a later snapshot it would describe a market the trade never saw.
    # ``None`` means the venue served no book ticker, and the reader falls back
    # to the configured constant rather than to a made-up number.
    spread_bps: Optional[float] = None

    # On-chain context as it stood *at signal time*. Recorded, never acted on:
    # none of these gate anything except whale_coordination, and the point of
    # storing them is to find out whether they should. Reading them off a later
    # snapshot would be useless — the question is whether what was knowable when
    # the signal fired predicted how it resolved.
    #
    # Empty/None means the collector was off, had not run, or its snapshot was
    # already stale — which is itself worth telling apart from "the data said
    # nothing", so the buckets below keep NO_DATA distinct from NEUTRAL.
    onchain_bias: str = ""              # SUPPORTS_LONG | SUPPORTS_SHORT | NEUTRAL | ""
    whale_stance: str = ""              # WITH | AGAINST | NEUTRAL | "" — relative to
                                        # this signal's own direction, so buckets
                                        # stay comparable across LONG and SHORT
    whale_net_wallets: int = 0
    coinbase_premium_pct: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.symbol}_{int(time.time() * 1000)}"
        self.reasons = list(self.reasons)[:3]
        if self.entry_mode.upper() == EntryMode.MOMENTUM_NOW.value and not self.activated:
            self.activated = True
            self.activated_at = self.activated_at or self.priced_at or self.created_at

    @property
    def priced_at(self) -> Optional[str]:
        """When the entry price was printed, for an entry taken at market.

        Normally the screener re-quotes at the live feed before sending, and
        this is ``None``: the entry was priced at ``created_at``, exactly as
        the timestamp says.

        It matters when that re-quote could not happen — the exchange returned
        no price — and the entry stays at what the detector read: ``closes[-1]``,
        the close of the last *closed* bar of its own timeframe. A 1h signal
        assembled at 10:07 then quotes the 10:00 price and is live from 10:00,
        not 10:07. Those seven minutes already moved for or against the quote,
        and grading from 10:07 would hand them over for free while hiding them
        from the stop.

        ``None`` too when the timeframe is unknown — the gap cannot be measured
        without knowing how long a bar is — leaving ``created_at`` in play.
        """
        if self.entry_quoted_live:
            return None  # quoted at created_at; there is no gap to close
        span = INTERVAL_MS.get(self.timeframe or "")
        if not span or not self.created_at:
            return None
        try:
            created = datetime.fromisoformat(self.created_at)
        except (TypeError, ValueError):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        floored = (int(created.timestamp() * 1000) // span) * span
        return datetime.fromtimestamp(floored / 1000, tz=timezone.utc).isoformat()

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
