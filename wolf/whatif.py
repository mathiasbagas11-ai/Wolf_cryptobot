"""Re-grade resolved signals under a different rule, on the same price history.

A rule change argued from first principles is a guess: advancing the stop as
rungs fill obviously pays on trades that would have drifted back to entry, and
obviously costs on trades that dip and then run, and nothing about the argument
says which happens more often. The candles that decided each trade are still
fetchable, so the question can simply be asked of them.

This replays the *same* signals through the *same* evaluator with one setting
changed, so the comparison isolates the rule. It is not a backtest: it invents
no entries and re-uses only setups the bot actually took.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, replace
from typing import Optional

from wolf.config import LadderSettings, TrackerSettings
from wolf.models import Status
from wolf.tracker import OUTCOMES_KEY, Tracker, _parse_iso, _replay_start_ms, _risk_pct

log = logging.getLogger("wolf.whatif")


@dataclass
class RuleResult:
    """What one rule would have returned over the replayed sample."""

    rule: str
    n: int
    mean_r: float
    total_r: float
    win_rate: float
    avg_win_r: float
    avg_loss_r: float

    def line(self) -> str:
        return (
            f"{self.rule:<10} n={self.n} meanR={self.mean_r:+.3f} totalR={self.total_r:+.2f} "
            f"wr={self.win_rate:.1f} avgWin={self.avg_win_r:+.2f}R avgLoss=-{self.avg_loss_r:.2f}R"
        )


def _regrade(tracker: Tracker, signals: list, ladder: LadderSettings) -> Optional[list[float]]:
    """R-multiples the sample would have produced under ``ladder``.

    ``None`` when price history could not be fetched for enough of the sample
    to compare — a partial answer here is worse than none, because the rules
    would be scored on different trades.
    """
    probe = Tracker(tracker._store, tracker._client, tracker._settings, ladder=ladder)
    out: list[float] = []
    missed = 0
    for sig in signals:
        try:
            created_at = _parse_iso(sig.created_at)
            resolved_at = _parse_iso(sig.exit_time or sig.resolved_at or sig.created_at)
            hours = max((resolved_at - created_at).total_seconds() / 3600, 1.0)
            candles = tracker._client.get_klines(
                sig.symbol, interval="15m", limit=int(hours * 4) + 20
            )
            start_ts = _replay_start_ms(sig, int(created_at.timestamp() * 1000))
            future = [c for c in candles if c.time >= start_ts]
            if not future:
                missed += 1
                continue
            fresh = replace(sig, status=Status.PENDING.value, tps_hit=[], exit_price=None,
                            exit_time=None, pnl_pct=None, r_multiple=None, resolved_at=None)
            res = probe._evaluate(fresh, future, created_at, resolved_at)
        except Exception:
            log.exception("Re-grade failed for %s", sig.symbol)
            missed += 1
            continue
        risk = _risk_pct(sig)
        pnl = res.realized_pnl_pct
        if pnl is None and res.exit_price and sig.entry_price > 0:
            pnl = (
                (res.exit_price - sig.entry_price) / sig.entry_price * 100
                if sig.is_long
                else (sig.entry_price - res.exit_price) / sig.entry_price * 100
            )
        if pnl is None or not risk:
            missed += 1
            continue
        out.append(pnl / risk)
    if missed > len(signals) / 2:
        return None
    return out


def _summarise(rule: str, rs: list[float]) -> RuleResult:
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    return RuleResult(
        rule=rule,
        n=len(rs),
        mean_r=round(statistics.fmean(rs), 3) if rs else 0.0,
        total_r=round(sum(rs), 2),
        win_rate=round(len(wins) / len(rs) * 100, 1) if rs else 0.0,
        avg_win_r=round(statistics.fmean(wins), 3) if wins else 0.0,
        avg_loss_r=round(statistics.fmean(losses), 3) if losses else 0.0,
    )


def compare_stop_rules(
    tracker: Tracker,
    rules: tuple[str, ...] = ("breakeven", "ladder"),
    limit: int = 200,
) -> dict:
    """Score each stop-advance rule over the same resolved signals."""
    raw = tracker._store.read(OUTCOMES_KEY, default=[]) or []
    from wolf.models import Signal

    signals = [Signal.from_dict(d) for d in raw if isinstance(d, dict)][-limit:]
    signals = [s for s in signals if Status(s.status).is_graded and s.tp_ladder]
    if not signals:
        return {"error": "no graded signals with a ladder to replay", "results": []}

    results = []
    for rule in rules:
        rs = _regrade(tracker, signals, replace(tracker._ladder, stop_advance=rule))
        if rs is None:
            return {
                "error": "price history unavailable for too much of the sample",
                "results": [],
            }
        results.append(_summarise(rule, rs))
    return {"error": "", "sample": len(signals), "results": results}


def render(report: dict) -> str:
    if report.get("error"):
        return f"WHATIF stop rules: {report['error']}"
    lines = [f"WHATIF stop rules | replayed {report['sample']} resolved signals"]
    lines += [r.line() for r in report["results"]]
    best = max(report["results"], key=lambda r: r.mean_r)
    spread = best.mean_r - min(r.mean_r for r in report["results"])
    lines.append(
        f"=> {best.rule} leads by {spread:+.3f}R/trade"
        if spread else "=> no difference on this sample"
    )
    return "\n".join(lines)
