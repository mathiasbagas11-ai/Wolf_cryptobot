"""Order-flow analytics — reading *who* is aggressive, not just how much traded.

Distinct from :mod:`wolf.flow`, which aggregates macro and on-chain flows for
the intelligence brief. This module works on candles: it measures the tape
of a single market over the last few bars.

The GMGN radar ranks on-chain pools with three ideas that transfer cleanly to
candle data, and one that does not:

======================  ==================================================
GMGN (on-chain)         Port used here
======================  ==================================================
``FLOW`` =              :func:`volume_flow` — volume in the recent window,
``(5m vol × 12) / 1h``  annualised to the baseline window's length and
                        divided by the baseline. ``1.0`` = current pace
                        matches the baseline.
``S×`` =                :func:`trade_flow` — identical arithmetic over
``(5m swaps × 12)/1h``  trade *counts* instead of notional.
buy/sell volume split   :func:`taker_bias` — taker-buy share of volume,
                        from the exchange's aggressor flag.
``V/L`` =               *not* ported to detectors. See :func:`turnover`.
``1h vol / liquidity``
======================  ==================================================

The load-bearing lesson from that project is :func:`flow_direction`: a hot
reading measures **activity, not safety**. GMGN only calls a move directional
when price *and* aggressive volume agree, with a margin so a near-even
buy/sell split is never labelled directional — otherwise a violent sell-off
looks identical to a breakout, since both print huge volume. Every detector in
this bot now routes its volume confirmation through that gate.

``V/L`` has no faithful candle-only equivalent: a CEX order book's depth is not
in the kline stream, so "volume relative to liquidity" needs a separate depth
snapshot. :func:`turnover` computes it when depth is supplied (the radar report
does this from one book-ticker call); detectors deliberately use the
baseline-relative :func:`volume_flow` instead of a fabricated liquidity number.

Everything here is a pure function over candles: no I/O, no globals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from wolf.models import Candle

__all__ = [
    "FlowState",
    "volume_flow",
    "trade_flow",
    "taker_bias",
    "flow_direction",
    "analyse",
    "turnover",
    "HOT",
    "ACTIVE",
    "COOLING",
    "COLD",
]

# Speed labels, mirroring the radar's 🔥/🟢/🟡/🧊 buckets.
HOT = "HOT"
ACTIVE = "ACTIVE"
COOLING = "COOLING"
COLD = "COLD"

_SPEED_ICONS = {HOT: "🔥", ACTIVE: "🟢", COOLING: "🟡", COLD: "🧊"}
_DIR_ICONS = {"BULLISH": "📈", "BEARISH": "📉", "MIXED": "🔄", "UNKNOWN": "❔"}

# GMGN's thresholds, kept as named constants so detectors read declaratively.
HOT_RATIO = 1.20
ACTIVE_RATIO = 0.80
COOLING_RATIO = 0.50

# Direction gate. Both are GMGN's: price must move more than a hair, and one
# side of the flow must lead the other by a clear margin.
MIN_PRICE_MOVE = 0.01   # 1% over the fast window
DOMINANCE_MARGIN = 1.05  # winning side must exceed the other by 5%


@dataclass(frozen=True)
class FlowState:
    """Everything the detectors need to know about short-term flow.

    ``NaN``/``UNKNOWN`` members mean *not measurable* (missing venue data or too
    few candles) and must be treated as neutral — never as a bearish or bullish
    reading.
    """

    volume_ratio: float = float("nan")   # FLOW
    trade_ratio: float = float("nan")    # S×
    buy_share: float = float("nan")      # 0-1 taker-buy share of volume
    price_change: float = float("nan")   # fractional change over the fast window
    direction: str = "UNKNOWN"           # BULLISH | BEARISH | MIXED | UNKNOWN
    speed: str = "UNKNOWN"               # HOT | ACTIVE | COOLING | COLD

    # ── speed ──
    @property
    def is_hot(self) -> bool:
        return self.speed == HOT

    @property
    def is_expanding(self) -> bool:
        """Volume running at or above its baseline pace."""
        return self.speed in (HOT, ACTIVE)

    @property
    def is_cold(self) -> bool:
        return self.speed == COLD

    # ── direction ──
    @property
    def is_bullish(self) -> bool:
        return self.direction == "BULLISH"

    @property
    def is_bearish(self) -> bool:
        return self.direction == "BEARISH"

    @property
    def is_measurable(self) -> bool:
        """True when the venue gave us enough data to judge direction."""
        return self.direction != "UNKNOWN"

    @property
    def price_direction(self) -> str:
        """Direction from price alone — the fallback when there is no split.

        Weaker than :attr:`direction` by design: it is half of GMGN's test, so
        callers must treat it as a hint worth partial credit and never as
        grounds for a veto.
        """
        if math.isnan(self.price_change):
            return "UNKNOWN"
        if self.price_change > MIN_PRICE_MOVE:
            return "BULLISH"
        if self.price_change < -MIN_PRICE_MOVE:
            return "BEARISH"
        return "MIXED"

    def agrees_with(self, is_long: bool) -> bool:
        """True when flow direction confirms a trade in the given direction."""
        return self.is_bullish if is_long else self.is_bearish

    def aggressor_opposes(self, is_long: bool) -> bool:
        """True when the aggressive side leads *against* the trade.

        Deliberately ignores price, unlike :attr:`direction`. A breakout is
        chosen because price is moving up, so the strict price-and-volume test
        can almost never call it bearish — yet price ticking up while sellers
        hit every bid is exactly the distribution-into-strength that fails.
        Judging the aggressor alone is what catches it.
        """
        if math.isnan(self.buy_share):
            return False
        buy, sell = self.buy_share, 1 - self.buy_share
        return (sell > buy * DOMINANCE_MARGIN) if is_long else (buy > sell * DOMINANCE_MARGIN)

    def conflicts_with(self, is_long: bool) -> bool:
        """True when flow points *against* the trade.

        Two ways that happens: the full GMGN reading is the opposite direction
        (price and volume agreeing the other way), or volume is expanding while
        the aggressive side leads against the trade. ``UNKNOWN`` is never a
        conflict — an unmeasurable tape is not evidence.
        """
        if not self.is_measurable:
            return False
        opposite = self.is_bearish if is_long else self.is_bullish
        return opposite or (self.is_expanding and self.aggressor_opposes(is_long))

    # ── display ──
    @property
    def icon(self) -> str:
        speed = _SPEED_ICONS.get(self.speed, "")
        return f"{speed}{_DIR_ICONS.get(self.direction, '')}"

    def label(self) -> str:
        """Compact ``🔥📈1.6``-style label, as printed by the GMGN board."""
        if math.isnan(self.volume_ratio):
            return "-"
        return f"{self.icon}{self.volume_ratio:.1f}"


def _speed(ratio: float) -> str:
    if math.isnan(ratio):
        return "UNKNOWN"
    if ratio > HOT_RATIO:
        return HOT
    if ratio >= ACTIVE_RATIO:
        return ACTIVE
    if ratio >= COOLING_RATIO:
        return COOLING
    return COLD


def _windows(
    candles: Sequence[Candle], fast: int, baseline: int
) -> Optional[tuple[Sequence[Candle], Sequence[Candle]]]:
    """Split the tail into ``(fast_window, baseline_window)``.

    The baseline *includes* the fast window, exactly as GMGN's rolling 1h volume
    includes the latest 5m: the ratio asks "is the current pace above the pace
    of the period it belongs to", which is what makes ``1.0`` mean "unchanged".
    """
    if fast <= 0 or baseline <= fast or len(candles) < baseline:
        return None
    return candles[-fast:], candles[-baseline:]


def volume_flow(candles: Sequence[Candle], fast: int = 4, baseline: int = 16) -> float:
    """GMGN ``FLOW``: the fast window's volume pace against the baseline's.

    Returns ``NaN`` when there is not enough data. ``1.0`` means the recent
    window is trading at exactly the baseline's average pace, ``2.0`` at double.
    """
    win = _windows(candles, fast, baseline)
    if win is None:
        return float("nan")
    fast_w, base_w = win
    base_vol = sum(c.volume for c in base_w)
    if base_vol <= 0:
        return float("nan")
    fast_vol = sum(c.volume for c in fast_w)
    # Scale the fast window up to the baseline's length, then compare.
    return (fast_vol * (baseline / fast)) / base_vol


def trade_flow(candles: Sequence[Candle], fast: int = 4, baseline: int = 16) -> float:
    """GMGN ``S×``: the same pace ratio over trade counts instead of notional.

    Diverges from :func:`volume_flow` when average trade size changes — many
    small fills (bot churn, retail panic) push ``S×`` up while ``FLOW`` stays
    flat, which the GMGN README flags as *not* a momentum entry.
    """
    win = _windows(candles, fast, baseline)
    if win is None:
        return float("nan")
    fast_w, base_w = win
    base_trades = sum(c.trades for c in base_w)
    if base_trades <= 0:
        return float("nan")  # venue does not publish trade counts
    fast_trades = sum(c.trades for c in fast_w)
    return (fast_trades * (baseline / fast)) / base_trades


def taker_bias(candles: Sequence[Candle], fast: int = 4) -> float:
    """Taker-buy share of volume over the fast window (0-1), or ``NaN``.

    ``0.5`` is balanced; above means aggressive buyers dominate. This is the
    CEX form of GMGN's ``buy_volume_5m`` vs ``sell_volume_5m`` comparison.
    """
    if fast <= 0 or len(candles) < fast:
        return float("nan")
    window = candles[-fast:]
    if not any(c.has_flow_data for c in window):
        return float("nan")
    total = sum(c.volume for c in window)
    if total <= 0:
        return float("nan")
    return sum(c.taker_buy_volume for c in window) / total


def price_change(candles: Sequence[Candle], fast: int = 4) -> float:
    """Fractional close-to-close change across the fast window."""
    if fast <= 0 or len(candles) < fast:
        return float("nan")
    window = candles[-fast:]
    start = window[0].open
    end = window[-1].close
    if start <= 0:
        return float("nan")
    return (end / start) - 1


def flow_direction(price_chg: float, buy_share: float) -> str:
    """Classify direction, requiring price and aggressive volume to agree.

    This is the heart of the GMGN port. A direction is only returned when:

    1. price moved more than :data:`MIN_PRICE_MOVE` over the window, and
    2. the matching side of taker volume leads the other by
       :data:`DOMINANCE_MARGIN`.

    Anything else is ``MIXED`` — price drifting without flow behind it, flow
    without price, or the two disagreeing. Missing data gives ``UNKNOWN``, which
    callers must treat as neutral, not as a veto.
    """
    if math.isnan(price_chg) or math.isnan(buy_share):
        return "UNKNOWN"
    sell_share = 1 - buy_share
    if price_chg > MIN_PRICE_MOVE and buy_share > sell_share * DOMINANCE_MARGIN:
        return "BULLISH"
    if price_chg < -MIN_PRICE_MOVE and sell_share > buy_share * DOMINANCE_MARGIN:
        return "BEARISH"
    return "MIXED"


def analyse(candles: Sequence[Candle], fast: int = 4, baseline: int = 16) -> FlowState:
    """Build the full :class:`FlowState` for a candle series.

    Defaults of ``fast=4``/``baseline=16`` on 15m candles read the last hour
    against the last four hours — the same 1:4 window proportion the radar uses
    for 5m against 1h.
    """
    vol_ratio = volume_flow(candles, fast, baseline)
    trd_ratio = trade_flow(candles, fast, baseline)
    buy_share = taker_bias(candles, fast)
    chg = price_change(candles, fast)
    return FlowState(
        volume_ratio=vol_ratio,
        trade_ratio=trd_ratio,
        buy_share=buy_share,
        price_change=chg,
        direction=flow_direction(chg, buy_share),
        speed=_speed(vol_ratio),
    )


def turnover(quote_volume: float, depth_usd: float) -> float:
    """GMGN ``V/L``: traded notional divided by resting liquidity.

    ``depth_usd`` must come from an order-book snapshot; there is no honest way
    to derive it from klines, which is why detectors do not use this and only
    the radar report — which fetches book tickers — does. A high value means
    fast turnover relative to the size of the book, not that the move is safe.
    """
    if depth_usd <= 0 or quote_volume < 0:
        return float("nan")
    return quote_volume / depth_usd
