"""Paper trading account — a simulated equity curve for the Trade Report.

The bot doesn't place real orders, so "saldo" is a *paper* balance: it starts at
a configured amount and compounds as signals resolve, risking a fixed percentage
of equity per trade. Each resolved signal moves the balance by
``risk_amount × R-multiple`` where R = pnl% / risk%. This gives an honest,
reproducible PnL-in-currency view (entry/exit/SL already live on the Signal)
without pretending to know a real exchange balance.

State is persisted through :class:`~wolf.state.StateStore` so the curve survives
restarts.
"""

from __future__ import annotations

import logging
from typing import Optional

from wolf.models import Signal, Status
from wolf.state import StateStore

log = logging.getLogger("wolf.account")

ACCOUNT_KEY = "paper_account"


class PaperAccount:
    def __init__(self, store: StateStore, start_balance: float = 1000.0, risk_pct: float = 1.0) -> None:
        self._store = store
        self._start = float(start_balance)
        self._risk_pct = max(0.0, float(risk_pct))

    def _state(self) -> dict:
        st = self._store.read(ACCOUNT_KEY, default=None)
        if not isinstance(st, dict) or "balance" not in st:
            st = {"balance": self._start, "trades": 0, "realized": 0.0, "peak": self._start}
            self._store.write(ACCOUNT_KEY, st)
        # Backfill the equity peak for accounts persisted before drawdown tracking.
        if "peak" not in st:
            st["peak"] = float(st["balance"])
            self._store.write(ACCOUNT_KEY, st)
        return st

    @property
    def balance(self) -> float:
        return float(self._state()["balance"])

    @property
    def peak(self) -> float:
        """Highest balance the equity curve has reached."""
        return float(self._state().get("peak", self._start))

    def drawdown_pct(self) -> float:
        """How far below its peak the balance sits, as a positive percent."""
        st = self._state()
        peak = float(st.get("peak", self._start))
        bal = float(st["balance"])
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - bal) / peak * 100)

    @staticmethod
    def risk_pct_of(signal: Signal) -> float:
        """Stop distance as a percent of entry — the trade's risk leg."""
        if not signal.entry_price or signal.sl is None:
            return 0.0
        return abs(signal.entry_price - signal.sl) / signal.entry_price * 100

    def apply(self, signal: Signal) -> Optional[dict]:
        """Settle a resolved ``signal`` against the paper balance.

        Returns a snapshot dict (balance, pnl_amount, risk_amount, r_multiple)
        for the Trade Report, or ``None`` for non-graded outcomes (e.g.
        INVALIDATED) that shouldn't touch equity.
        """
        status = Status(signal.status)
        if not (status.is_win or status.is_loss):
            return None

        st = self._state()
        balance = float(st["balance"])
        risk_amount = balance * (self._risk_pct / 100)
        risk_leg = self.risk_pct_of(signal)
        pnl_pct = signal.pnl_pct or 0.0
        r_multiple = (pnl_pct / risk_leg) if risk_leg else 0.0
        pnl_amount = risk_amount * r_multiple

        balance += pnl_amount
        st["balance"] = round(balance, 2)
        st["peak"] = round(max(float(st.get("peak", balance)), balance), 2)
        st["trades"] = int(st.get("trades", 0)) + 1
        st["realized"] = round(float(st.get("realized", 0.0)) + pnl_amount, 2)
        self._store.write(ACCOUNT_KEY, st)

        return {
            "balance": st["balance"],
            "pnl_amount": round(pnl_amount, 2),
            "risk_amount": round(risk_amount, 2),
            "r_multiple": round(r_multiple, 2),
        }
