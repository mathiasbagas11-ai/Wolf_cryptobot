"""Trade-plan / position-sizing engine.

Turns a signal's price geometry (entry + stop) into an *executable* plan for a
beginner, implementing the risk rules from the trading guide with correct
futures math:

* **Fixed-fractional risk** — size the position so that hitting the stop costs
  exactly ``risk_pct`` of the account, regardless of how far the stop is. This is
  the guide's "kalau SL kena, rugi lo cuma 2%" rule, done properly: notional is
  derived from the stop distance, not guessed.
* **Liquidation-safe leverage** — recommend the *largest* leverage (capped) whose
  isolated liquidation price still sits a safety buffer beyond the stop, so the
  stop is always hit first. Using leverage that liquidates before the stop is the
  #1 beginner blow-up the guide warns about.

Isolated USDⓈ-M liquidation (fees ignored, maintenance-margin ``mmr``):

    long :  liq = entry * (1 - 1/lev + mmr)
    short:  liq = entry * (1 + 1/lev - mmr)

so the liquidation distance from entry is ``(1/lev - mmr)`` of price either way.
Solving ``(1/lev - mmr) >= buffer * stop_dist`` gives the safe leverage ceiling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TradePlan:
    leverage: int
    margin: float          # USD margin to commit (isolated)
    margin_pct: float      # margin as % of balance
    notional: float        # position size in USD
    risk_amount: float     # USD lost if the stop is hit
    risk_pct: float        # that loss as % of balance
    stop_dist_pct: float   # |entry - sl| / entry, percent
    liq_price: float
    liq_dist_pct: float    # liquidation distance from entry, percent
    liq_safe: bool         # is the stop comfortably inside liquidation?


def build_plan(
    entry: float,
    sl: float,
    is_long: bool,
    balance: float,
    risk_pct: float,
    max_leverage: int = 10,
    mmr: float = 0.005,
    buffer: float = 2.0,
) -> Optional[TradePlan]:
    """Build a :class:`TradePlan`, or ``None`` if inputs are unusable."""
    if entry <= 0 or sl <= 0 or balance <= 0 or risk_pct <= 0:
        return None
    stop_dist = abs(entry - sl) / entry
    if stop_dist <= 0 or math.isnan(stop_dist):
        return None

    # Largest leverage whose liquidation stays ``buffer`` × the stop away.
    denom = buffer * stop_dist + mmr
    raw_max = (1.0 / denom) if denom > 0 else float(max_leverage)
    leverage = int(max(1, min(max_leverage, math.floor(raw_max))))

    liq_dist = (1.0 / leverage) - mmr  # fraction of price
    liq_safe = liq_dist >= buffer * stop_dist
    liq_price = entry * (1 - liq_dist) if is_long else entry * (1 + liq_dist)

    risk_amount = balance * risk_pct / 100.0
    notional = risk_amount / stop_dist
    margin = notional / leverage

    return TradePlan(
        leverage=leverage,
        margin=round(margin, 2),
        margin_pct=round(margin / balance * 100, 2),
        notional=round(notional, 2),
        risk_amount=round(risk_amount, 2),
        risk_pct=round(risk_pct, 2),
        stop_dist_pct=round(stop_dist * 100, 2),
        liq_price=liq_price,
        liq_dist_pct=round(liq_dist * 100, 2),
        liq_safe=liq_safe,
    )


def render_plan(plan: TradePlan, balance: float, fmt_price) -> str:
    """Render a plan as Telegram HTML lines (no trailing newline)."""
    safe = "✅ aman (SL kena lebih dulu)" if plan.liq_safe else "⚠️ RISIKO: perlebar SL / kecilkan leverage"
    return (
        f"📋 <b>TRADE PLAN</b> (saldo ${balance:,.0f}, risk {plan.risk_pct:.0f}%, isolated)\n"
        f"• Leverage: <b>{plan.leverage}x</b> · Margin ${plan.margin:,.2f} ({plan.margin_pct:.1f}%)\n"
        f"• Ukuran posisi ${plan.notional:,.2f} · Risk ${plan.risk_amount:,.2f}\n"
        f"• Lik. ≈ <code>{fmt_price(plan.liq_price)}</code> (−{plan.liq_dist_pct:.1f}%) "
        f"vs SL −{plan.stop_dist_pct:.1f}% {safe}\n"
    )
