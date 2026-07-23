"""Lightweight backtest engine.

Replays the *live* detectors over recent history so the bot can estimate each
strategy's edge — and warm-start the learning memory so it doesn't begin blind.
For every symbol it walks the last ``lookback`` closed candles; at each bar it
asks the detectors for their best candidate using only candles up to that bar
(no look-ahead), then simulates the trade forward with the same TP-ladder /
breakeven rules the tracker uses live.

Reuses the production detectors, :class:`~wolf.indicator_cache.CandleFeatures`
and :func:`~wolf.tracker.normalize_ladder`, so backtest and live can't drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from wolf.detectors.base import Detector, SignalCandidate
from wolf.indicator_cache import CandleFeatures
from wolf.models import Candle
from wolf.tracker import normalize_ladder

log = logging.getLogger("wolf.backtest")


@dataclass
class SimTrade:
    strategy: str
    symbol: str
    direction: str
    win: bool
    pnl_pct: float
    r_multiple: float
    status: str


def simulate(candidate: SignalCandidate, future: Sequence[Candle]) -> Optional[SimTrade]:
    """Simulate one candidate forward; ``None`` if the entry was never touched."""
    is_long = candidate.direction.upper() == "LONG"
    entry = candidate.entry_price
    if entry <= 0 or candidate.sl <= 0:
        return None
    ladder = normalize_ladder(candidate.tps, candidate.tp, candidate.sl, entry, is_long)
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
            exit_price, status = eff_sl, "SL_HIT"
            break
        for rung in ladder:
            if rung.level in tps_hit:
                continue
            if (is_long and c.high >= rung.price) or (not is_long and c.low <= rung.price):
                tps_hit.add(rung.level)
                if rung.level == first_lvl:
                    eff_sl = entry  # breakeven after TP1, matching the tracker
        if ladder and len(tps_hit) >= len(ladder):
            exit_price, status = ladder[-1].price, "TP_HIT"
            break

    if not activated:
        return None
    if exit_price is None:
        exit_price = future[-1].close if future else entry

    pnl = (exit_price - entry) / entry * 100 if is_long else (entry - exit_price) / entry * 100
    stop_dist = abs(entry - candidate.sl) / entry * 100
    r = pnl / stop_dist if stop_dist else 0.0
    return SimTrade(candidate.strategy, candidate.symbol, candidate.direction,
                    pnl > 0, round(pnl, 3), round(r, 2), status)


class BacktestEngine:
    def __init__(self, client, detectors: Sequence[Detector], interval: str = "15m",
                 lookback: int = 50, candle_limit: int = 250) -> None:
        self._client = client
        self._detectors = list(detectors)
        self._interval = interval
        self._lookback = max(10, lookback)
        self._candle_limit = candle_limit

    def _best(self, symbol: str, history: Sequence[Candle]) -> Optional[SignalCandidate]:
        try:
            features = CandleFeatures.build(history)
        except Exception:
            features = None
        best: Optional[SignalCandidate] = None
        for det in self._detectors:
            try:
                cand = det.evaluate(symbol, history, None, features)
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
        cooldown_until = -1
        for i in range(len(candles) - self._lookback, len(candles) - 1):
            if i < cooldown_until:
                continue
            cand = self._best(symbol, candles[: i + 1])
            if not cand:
                continue
            sim = simulate(cand, candles[i + 1:])
            if sim is None:
                continue
            trades.append(sim)
            cooldown_until = i + 4
        return trades

    def run(self, symbols: Sequence[str]) -> dict:
        all_trades: list[SimTrade] = []
        for sym in symbols:
            all_trades.extend(self.run_symbol(sym))
        by_strategy: dict[str, dict] = {}
        for t in all_trades:
            b = by_strategy.setdefault(t.strategy, {"trades": 0, "wins": 0, "pnl_sum": 0.0, "r_sum": 0.0})
            b["trades"] += 1
            b["wins"] += 1 if t.win else 0
            b["pnl_sum"] += t.pnl_pct
            b["r_sum"] += t.r_multiple
        for b in by_strategy.values():
            t = b["trades"]
            b["win_rate"] = round(b["wins"] / t * 100, 1) if t else 0.0
            b["avg_pnl"] = round(b["pnl_sum"] / t, 3) if t else 0.0
            b["avg_r"] = round(b["r_sum"] / t, 2) if t else 0.0
        return {"total_trades": len(all_trades), "by_strategy": by_strategy, "trades": all_trades}
