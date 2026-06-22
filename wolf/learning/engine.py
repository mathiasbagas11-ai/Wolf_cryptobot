"""Adaptive learning engine — the bot's memory.

Every resolved trade is folded into a persistent memory of how each *strategy*
and each *symbol* has actually performed. That memory feeds back into live
screening two ways:

* **Score adjustment** — a strategy (or symbol) with a strong realised win-rate
  gets a small score *bonus*; a poor one gets a *penalty*. The nudge is bounded
  (``max_adjust``) and only applies once there is a meaningful sample
  (``min_samples``), so a couple of unlucky trades can't lobotomise a strategy.
* **Blacklist** — a symbol that has traded enough times with a dismal win-rate is
  temporarily benched so the bot stops feeding it losers.

This complements main's per-strategy "lesson" line (which only narrates): here the
memory is *per-symbol too* and actually changes the score and can skip a symbol.
Win/loss is graded by the terminal :class:`~wolf.models.Status`, matching the
rest of the bot's accounting. File-backed via the shared state store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from wolf.config import LearningSettings
from wolf.models import Signal, Status
from wolf.state import StateStore

log = logging.getLogger("wolf.learning")

MEMORY_KEY = "learning_memory"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _blank() -> dict:
    return {"trades": 0, "wins": 0, "pnl_sum": 0.0, "r_sum": 0.0}


def _r_multiple(sig: Signal) -> float:
    risk_leg = abs(sig.entry_price - sig.sl) / sig.entry_price * 100 if sig.entry_price and sig.sl else 0.0
    return (sig.pnl_pct or 0.0) / risk_leg if risk_leg else 0.0


@dataclass
class Adjustment:
    delta: float = 0.0
    blacklisted: bool = False
    reason: str = ""


class LearningEngine:
    def __init__(self, store: StateStore, settings: LearningSettings) -> None:
        self._store = store
        self._settings = settings

    # ── recording ────────────────────────────────────────────────────────
    def observe(self, sig: Signal) -> None:
        """Fold one resolved, graded trade into memory (win/loss by status)."""
        if not self._settings.enabled:
            return
        try:
            status = Status(sig.status)
        except ValueError:
            return
        if not (status.is_win or status.is_loss):
            return
        win = status.is_win
        r = _r_multiple(sig)
        pnl = sig.pnl_pct or 0.0

        def _mutator(mem):
            mem = mem or {"strategies": {}, "symbols": {}}
            for scope, key in (("strategies", sig.strategy or "UNKNOWN"), ("symbols", sig.symbol)):
                bucket = mem[scope].setdefault(key, _blank())
                bucket["trades"] += 1
                bucket["wins"] += 1 if win else 0
                bucket["pnl_sum"] += pnl
                bucket["r_sum"] += r
            return mem

        self._store.update(MEMORY_KEY, _mutator, default={"strategies": {}, "symbols": {}})

    def seed(self, trades) -> None:
        """Warm-start memory from backtested ``(strategy, symbol, win, pnl, r)``."""
        if not self._settings.enabled or not trades:
            return

        def _mutator(mem):
            mem = mem or {"strategies": {}, "symbols": {}}
            for strategy, symbol, win, pnl, r in trades:
                for scope, key in (("strategies", strategy or "UNKNOWN"), ("symbols", symbol)):
                    bucket = mem[scope].setdefault(key, _blank())
                    bucket["trades"] += 1
                    bucket["wins"] += 1 if win else 0
                    bucket["pnl_sum"] += pnl
                    bucket["r_sum"] += r
            return mem

        self._store.update(MEMORY_KEY, _mutator, default={"strategies": {}, "symbols": {}})

    # ── querying ─────────────────────────────────────────────────────────
    def _memory(self) -> dict:
        return self._store.read(MEMORY_KEY, default={"strategies": {}, "symbols": {}})

    def _scope_delta(self, bucket: Optional[dict], weight: float) -> tuple[float, float]:
        if not bucket or bucket["trades"] < self._settings.min_samples:
            return 0.0, -1.0
        wr = bucket["wins"] / bucket["trades"]
        delta = (wr - 0.5) * 2 * self._settings.max_adjust * weight
        return delta, wr

    def adjustment(self, symbol: str, strategy: str) -> Adjustment:
        """Compute the live score adjustment + blacklist flag for a candidate."""
        if not self._settings.enabled:
            return Adjustment()
        mem = self._memory()
        sym_bucket = mem.get("symbols", {}).get(symbol)
        strat_bucket = mem.get("strategies", {}).get(strategy)

        if sym_bucket and sym_bucket["trades"] >= self._settings.blacklist_min_trades:
            wr = sym_bucket["wins"] / sym_bucket["trades"]
            if wr * 100 < self._settings.blacklist_max_winrate:
                return Adjustment(
                    blacklisted=True,
                    reason=f"{symbol} benched: {wr*100:.0f}% WR over {sym_bucket['trades']} trades",
                )

        strat_delta, strat_wr = self._scope_delta(strat_bucket, weight=1.0)
        sym_delta, sym_wr = self._scope_delta(sym_bucket, weight=0.5)
        delta = _clamp(strat_delta + sym_delta, -self._settings.max_adjust, self._settings.max_adjust)
        if abs(delta) < 0.5:
            return Adjustment()
        bits = []
        if strat_wr >= 0:
            bits.append(f"{strategy} {strat_wr*100:.0f}%WR")
        if sym_wr >= 0:
            bits.append(f"{symbol} {sym_wr*100:.0f}%WR")
        verb = "boost" if delta > 0 else "penalty"
        return Adjustment(delta=delta, reason=f"Learning {verb} {delta:+.0f} ({', '.join(bits)})")

    def symbol_edge(self, symbol: str) -> Optional[dict]:
        b = self._memory().get("symbols", {}).get(symbol)
        if not b or not b["trades"]:
            return None
        t = b["trades"]
        return {"trades": t, "win_rate": round(b["wins"] / t * 100, 1),
                "avg_pnl": round(b["pnl_sum"] / t, 3), "avg_r": round(b["r_sum"] / t, 2)}

    def snapshot(self) -> dict:
        """Human-readable view of memory for reports / the API / commands."""
        mem = self._memory()
        out: dict = {"strategies": {}, "symbols": {}, "blacklist": []}
        for scope in ("strategies", "symbols"):
            for key, b in mem.get(scope, {}).items():
                t = b["trades"]
                row = {
                    "trades": t,
                    "win_rate": round(b["wins"] / t * 100, 1) if t else 0.0,
                    "avg_pnl": round(b["pnl_sum"] / t, 3) if t else 0.0,
                    "avg_r": round(b["r_sum"] / t, 2) if t else 0.0,
                }
                out[scope][key] = row
                if (scope == "symbols" and t >= self._settings.blacklist_min_trades
                        and row["win_rate"] < self._settings.blacklist_max_winrate):
                    out["blacklist"].append(key)
        return out
