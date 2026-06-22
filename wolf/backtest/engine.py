"""Lightweight backtest engine.

Replays the *live* detectors over recent history so the bot can estimate each
strategy's edge before (and alongside) trading it. For every symbol it walks the
last ``lookback`` closed candles; at each bar it asks the detectors for their
best candidate using only the candles available up to that bar (no look-ahead),
then simulates the trade forward with the exact same TP-ladder / breakeven /
scale-out rules the tracker uses live. The result is a per-strategy win-rate and
average PnL, plus a flat list of simulated trades that can *warm-start* the
learning memory so the bot doesn't begin completely blind.

It reuses the production detectors and :func:`~wolf.tracker.normalize_ladder`, so
backtest and live can never silently diverge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from wolf.detectors.base import Detector, SignalCandidate
from wolf.models import Candle
from wolf.tracker import normalize_ladder

log = logging.getLogger("wolf.backtest")


@dataclass
class SimTrade:
    strategy: str
    symbol: str
    direction: str
    pnl_pct: float
    r_multiple: float
    status: str


def simulate(candidate: SignalCandidate, future: Sequence[Candle]) -> Optional[SimTrade]:
    """Simulate one candidate forward over ``future`` candles.

    Returns ``None`` if the entry was never touched (no risk taken). Otherwise a
    :class:`SimTrade` with the net scale-out PnL and its R-multiple.
    """
    is_long = candidate.direction.upper() == "LONG"
    entry = candidate.entry_price
    if entry <= 0 or candidate.sl <= 0:
        return None
    ladder = normalize_ladder(candidate.tps, candidate.tp, candidate.sl, entry, is_long)
    n = len(ladder)
    activated = candidate.entry_mode.upper() == "MOMENTUM_NOW"
    eff_sl = candidate.sl
    tps_hit: set[int] = set()
    first_lvl = ladder[0].level if ladder else None
    exit_price: Optional[float] = None
    status = "EXPIRE"

    for c in future:
        if not activated:
            if (is_long and c.low <= entry) or (not is_long and c.high >= entry):
                activated = True
            else:
                continue
        if (is_long and c.low <= eff_sl) or (not is_long and c.high >= eff_sl):
            exit_price, status = eff_sl, "SL"
            break
        for rung in ladder:
            if rung.level in tps_hit:
                continue
            if (is_long and c.high >= rung.price) or (not is_long and c.low <= rung.price):
                tps_hit.add(rung.level)
                if rung.level == first_lvl:
                    eff_sl = entry
        if n and len(tps_hit) >= n:
            exit_price, status = ladder[-1].price, "TP"
            break

    if not activated:
        return None
    if exit_price is None:  # timed out — close at the last available price
        exit_price = future[-1].close if future else entry

    def _gain(price: float) -> float:
        return (price - entry) / entry * 100 if is_long else (entry - price) / entry * 100

    if n == 0:
        pnl = _gain(exit_price)
    else:
        frac = 1.0 / n
        pnl = 0.0
        closed = 0
        for rung in ladder:
            if rung.level in tps_hit:
                pnl += frac * _gain(rung.price)
                closed += 1
        if n - closed > 0:
            pnl += frac * (n - closed) * _gain(exit_price)

    stop_dist = abs(entry - candidate.sl) / entry * 100
    r = pnl / stop_dist if stop_dist else 0.0
    return SimTrade(candidate.strategy, candidate.symbol, candidate.direction,
                    round(pnl, 3), round(r, 2), status)


class BacktestEngine:
    def __init__(
        self,
        client,
        detectors: Sequence[Detector],
        interval: str = "15m",
        lookback: int = 50,
        candle_limit: int = 250,
    ) -> None:
        self._client = client
        self._detectors = list(detectors)
        self._interval = interval
        self._lookback = max(10, lookback)
        self._candle_limit = candle_limit

    def _best(self, symbol: str, history: Sequence[Candle]) -> Optional[SignalCandidate]:
        best: Optional[SignalCandidate] = None
        for det in self._detectors:
            try:
                cand = det.evaluate(symbol, history, None)
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            if cand and (best is None or cand.score > best.score):
                best = cand
        return best

    def run_symbol(self, symbol: str) -> list[SimTrade]:
        candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        if len(candles) < self._lookback + 30:
            return []
        trades: list[SimTrade] = []
        start = len(candles) - self._lookback
        cooldown_until = -1  # bar index until which we skip new signals (dedup)
        for i in range(start, len(candles) - 1):
            if i < cooldown_until:
                continue
            history = candles[: i + 1]
            cand = self._best(symbol, history)
            if not cand:
                continue
            sim = simulate(cand, candles[i + 1:])
            if sim is None:
                continue
            trades.append(sim)
            cooldown_until = i + 4  # avoid stacking near-identical bars
        return trades

    def run(self, symbols: Sequence[str]) -> dict:
        """Backtest a universe; return aggregated per-strategy stats + raw trades."""
        all_trades: list[SimTrade] = []
        for sym in symbols:
            all_trades.extend(self.run_symbol(sym))
        by_strategy: dict[str, dict] = {}
        for t in all_trades:
            b = by_strategy.setdefault(t.strategy, {"trades": 0, "wins": 0, "pnl_sum": 0.0, "r_sum": 0.0})
            b["trades"] += 1
            b["wins"] += 1 if t.pnl_pct > 0 else 0
            b["pnl_sum"] += t.pnl_pct
            b["r_sum"] += t.r_multiple
        for b in by_strategy.values():
            t = b["trades"]
            b["win_rate"] = round(b["wins"] / t * 100, 1) if t else 0.0
            b["avg_pnl"] = round(b["pnl_sum"] / t, 3) if t else 0.0
            b["avg_r"] = round(b["r_sum"] / t, 2) if t else 0.0
        return {
            "total_trades": len(all_trades),
            "by_strategy": by_strategy,
            "trades": all_trades,
        }
