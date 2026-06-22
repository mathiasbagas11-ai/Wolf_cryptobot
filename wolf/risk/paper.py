"""Paper-trading account — the bot's own simulated book.

Every signal the bot resolves is turned into a *paper* trade against a virtual
balance, sized by a fixed fractional-risk rule (risk a fixed % of equity on the
distance to stop). This gives two things the raw win-rate can't:

* **R-multiples** — PnL expressed in units of risk, the only PnL number that is
  comparable across symbols and volatility regimes.
* **A USD equity curve** — so the learning engine (and the operator) can see
  whether the edge actually compounds, including the drag of losers.

Sizing (fixed-fractional):

    risk_amount   = balance * risk_pct/100
    notional      = risk_amount / (stop_distance_pct/100)
    pnl_usd       = notional * net_pnl_pct/100   ==   risk_amount * R
    R             = net_pnl_pct / stop_distance_pct

Only *activated* trades are booked — a signal whose entry was never touched
(INVALIDATED) never put risk on, so it doesn't move the balance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from wolf.config import RiskSettings
from wolf.models import Signal
from wolf.state import StateStore

log = logging.getLogger("wolf.risk")

ACCOUNT_KEY = "paper_account"
TRADES_KEY = "paper_trades"
TRADES_CAP = 500


@dataclass
class PaperFill:
    """The result of booking one paper trade."""

    r_multiple: float
    pnl_usd: float
    balance: float


class PaperTrader:
    def __init__(self, store: StateStore, settings: RiskSettings) -> None:
        self._store = store
        self._settings = settings

    def _account(self) -> dict:
        return self._store.read(
            ACCOUNT_KEY,
            default={"balance": self._settings.starting_balance,
                     "peak": self._settings.starting_balance,
                     "trades": 0},
        )

    def balance(self) -> float:
        return float(self._account()["balance"])

    def record(self, sig: Signal) -> Optional[PaperFill]:
        """Book a resolved signal as a paper trade; returns the fill or None."""
        if not self._settings.paper_enabled:
            return None
        if not sig.activated or sig.pnl_pct is None or sig.entry_price <= 0 or sig.sl <= 0:
            return None
        stop_dist_pct = abs(sig.entry_price - sig.sl) / sig.entry_price * 100
        if stop_dist_pct <= 0:
            return None

        r_multiple = sig.pnl_pct / stop_dist_pct

        def _mutator(acct):
            acct = acct or {"balance": self._settings.starting_balance,
                            "peak": self._settings.starting_balance, "trades": 0}
            risk_amount = acct["balance"] * self._settings.risk_pct / 100
            pnl_usd = risk_amount * r_multiple
            acct["balance"] = round(acct["balance"] + pnl_usd, 2)
            acct["peak"] = round(max(acct["peak"], acct["balance"]), 2)
            acct["trades"] += 1
            acct["_last_pnl_usd"] = round(pnl_usd, 2)  # transient, read back below
            return acct

        acct = self._store.update(ACCOUNT_KEY, _mutator)
        pnl_usd = acct.pop("_last_pnl_usd", 0.0)
        # Drop the transient field from persisted state.
        self._store.write(ACCOUNT_KEY, acct)

        fill = PaperFill(round(r_multiple, 2), round(pnl_usd, 2), acct["balance"])
        self._store.update(
            TRADES_KEY,
            lambda cur: ((cur or []) + [{
                "symbol": sig.symbol,
                "strategy": sig.strategy,
                "direction": sig.direction,
                "status": sig.status,
                "pnl_pct": sig.pnl_pct,
                "r": fill.r_multiple,
                "pnl_usd": fill.pnl_usd,
                "balance": fill.balance,
                "resolved_at": sig.resolved_at,
            }])[-TRADES_CAP:],
            default=[],
        )
        log.info("Paper %s %s: %+.2fR %+.2f USD -> balance %.2f",
                 sig.symbol, sig.status, fill.r_multiple, fill.pnl_usd, fill.balance)
        return fill

    def stats(self) -> dict:
        acct = self._account()
        trades = self._store.read(TRADES_KEY, default=[])
        rs = [t["r"] for t in trades if isinstance(t, dict) and t.get("r") is not None]
        start = self._settings.starting_balance
        bal = acct["balance"]
        peak = acct.get("peak", bal)
        return {
            "balance": round(bal, 2),
            "starting_balance": start,
            "return_pct": round((bal - start) / start * 100, 2) if start else 0.0,
            "peak": round(peak, 2),
            "max_drawdown_pct": round((peak - bal) / peak * 100, 2) if peak else 0.0,
            "trades": acct.get("trades", 0),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else 0.0,
            "total_r": round(sum(rs), 2),
        }
