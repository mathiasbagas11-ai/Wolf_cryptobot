"""Adaptive learning engine — the bot's memory.

Every resolved trade is folded into a persistent memory of how each *strategy*
and each *symbol* has actually performed. That memory then feeds back into live
screening in two ways:

* **Score adjustment** — a strategy (or symbol) with a strong realised win-rate
  gets a small score *bonus*; a poor one gets a *penalty*. The nudge is bounded
  (``max_adjust``) and only applies once there is a meaningful sample
  (``min_samples``), so a couple of unlucky trades can't lobotomise a strategy.
* **Blacklist** — a symbol that has traded enough times with a dismal win-rate is
  temporarily benched so the bot stops feeding it losers.

The engine is deliberately simple, deterministic and file-backed (via the shared
:class:`~wolf.state.StateStore`), so its behaviour is easy to reason about and to
unit-test — in contrast to the old bot's sprawling, AI-coupled learning code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from wolf.config import LearningSettings
from wolf.models import Signal
from wolf.state import StateStore

log = logging.getLogger("wolf.learning")

MEMORY_KEY = "learning_memory"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _blank() -> dict:
    return {"trades": 0, "wins": 0, "pnl_sum": 0.0, "r_sum": 0.0}


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
    def observe(self, sig: Signal, r_multiple: Optional[float] = None) -> None:
        """Fold one resolved, activated trade into memory.

        Trades that never activated (INVALIDATED) or booked exactly zero PnL are
        ignored — they carry no signal about strategy/symbol quality.
        """
        if not self._settings.enabled:
            return
        if not sig.activated or sig.pnl_pct is None or sig.pnl_pct == 0.0:
            return
        win = sig.pnl_pct > 0

        def _mutator(mem):
            mem = mem or {"strategies": {}, "symbols": {}}
            for scope, key in (("strategies", sig.strategy or "UNKNOWN"), ("symbols", sig.symbol)):
                bucket = mem[scope].setdefault(key, _blank())
                bucket["trades"] += 1
                bucket["wins"] += 1 if win else 0
                bucket["pnl_sum"] += sig.pnl_pct
                if r_multiple is not None:
                    bucket["r_sum"] += r_multiple
            return mem

        self._store.update(MEMORY_KEY, _mutator, default={"strategies": {}, "symbols": {}})

    def seed(self, trades) -> None:
        """Warm-start memory from backtested trades ``(strategy, symbol, pnl, r)``.

        Used at boot so the bot doesn't trade its first live signals completely
        blind. Folds the whole batch in a single locked update.
        """
        if not self._settings.enabled or not trades:
            return

        def _mutator(mem):
            mem = mem or {"strategies": {}, "symbols": {}}
            for strategy, symbol, pnl, r in trades:
                if pnl is None or pnl == 0.0:
                    continue
                win = pnl > 0
                for scope, key in (("strategies", strategy or "UNKNOWN"), ("symbols", symbol)):
                    bucket = mem[scope].setdefault(key, _blank())
                    bucket["trades"] += 1
                    bucket["wins"] += 1 if win else 0
                    bucket["pnl_sum"] += pnl
                    if r is not None:
                        bucket["r_sum"] += r
            return mem

        self._store.update(MEMORY_KEY, _mutator, default={"strategies": {}, "symbols": {}})

    # ── querying ─────────────────────────────────────────────────────────
    def _memory(self) -> dict:
        return self._store.read(MEMORY_KEY, default={"strategies": {}, "symbols": {}})

    def _scope_delta(self, bucket: Optional[dict], weight: float) -> tuple[float, float]:
        """Return (delta, win_rate) for one memory bucket; (0, -1) if too small."""
        if not bucket or bucket["trades"] < self._settings.min_samples:
            return 0.0, -1.0
        wr = bucket["wins"] / bucket["trades"]
        # Centre on 50%: above lifts the score, below cuts it. ``scale`` maps a
        # full +/-50% win-rate swing onto the configured adjustment band.
        delta = (wr - 0.5) * 2 * self._settings.max_adjust * weight
        return delta, wr

    def adjustment(self, symbol: str, strategy: str) -> Adjustment:
        """Compute the live score adjustment + blacklist flag for a candidate."""
        if not self._settings.enabled:
            return Adjustment()
        mem = self._memory()
        sym_bucket = mem.get("symbols", {}).get(symbol)
        strat_bucket = mem.get("strategies", {}).get(strategy)

        # Blacklist: enough trades on this symbol with a dismal win-rate.
        if sym_bucket and sym_bucket["trades"] >= self._settings.blacklist_min_trades:
            wr = sym_bucket["wins"] / sym_bucket["trades"]
            if wr * 100 < self._settings.blacklist_max_winrate:
                return Adjustment(
                    delta=0.0, blacklisted=True,
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

    def snapshot(self) -> dict:
        """Human-readable view of memory for reports / the API."""
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
