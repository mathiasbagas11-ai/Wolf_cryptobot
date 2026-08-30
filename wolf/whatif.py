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
import math
import statistics
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from wolf.config import LadderSettings, TrackerSettings
from wolf.models import Status
from wolf.tracker import (
    OUTCOMES_KEY, Tracker, _parse_iso, _partial_pnl, _replay_start_ms, _resolved_at,
    _risk_pct, normalize_ladder, r_multiple_of,
)

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


#: A 15m bar is 900s, and exchanges serve klines counted back from *now*, so
#: reaching a signal's own window means asking for every bar since. Past this
#: many the request stops being served, and the signal is skipped rather than
#: replayed against whatever came back.
_MAX_BARS = 1000
_BAR_MS = 900_000


def _history(tracker: Tracker, sig) -> Optional[list]:
    """The candles this signal actually lived through, or ``None``.

    The klines API takes a count, not a date range: it answers with the most
    recent ``limit`` bars. Asking for as many bars as the trade *lasted* — two
    hours for a trade held two hours — therefore returns the last two hours of
    today, and every one of them passes a "after the signal started" filter if
    the signal is older than that. The replay then grades a week-old setup
    against this afternoon's price.

    So the count is measured from now back to the signal's start, and the first
    bar returned is checked against that start. If the history does not reach,
    the signal is dropped: no answer is better than an answer computed from the
    wrong prices.
    """
    created_at = _parse_iso(sig.created_at)
    start_ts = _replay_start_ms(sig, int(created_at.timestamp() * 1000))
    now = datetime.now(timezone.utc)
    bars_back = int((now.timestamp() * 1000 - start_ts) // _BAR_MS) + 10
    if bars_back > _MAX_BARS or bars_back <= 0:
        return None
    candles = tracker._client.get_klines(sig.symbol, interval="15m", limit=bars_back)
    if not candles or candles[0].time > start_ts:
        return None  # the window opens before anything we were served
    return [c for c in candles if c.time >= start_ts]


def _regrade_one(probe: Tracker, sig, candles: list) -> Optional[float]:
    """R-multiple this signal would have returned, replayed over ``candles``."""
    created_at = _parse_iso(sig.created_at)
    try:
        res = probe._evaluate(
            replace(sig, status=Status.PENDING.value, tps_hit=[], exit_price=None,
                    exit_time=None, pnl_pct=None, r_multiple=None, resolved_at=None),
            candles, created_at, datetime.now(timezone.utc),
        )
    except Exception:
        log.exception("Re-grade failed for %s", sig.symbol)
        return None
    risk = _risk_pct(sig)
    if not risk:
        return None
    pnl = res.realized_pnl_pct
    if pnl is None and res.terminal is None:
        # Still open at the end of the history. Dropping it would not be
        # neutral: a stop that never advances is exactly what keeps a trade
        # running, so the unresolved rows are the ones where the rules differ
        # most, and excluding them would quietly grade each rule on the trades
        # that suit it. Value it where it stands instead — the rungs already
        # banked at their own prices, the remainder at the last close.
        ladder = normalize_ladder(sig.tp_ladder, sig.tp, sig.sl, sig.entry_price, sig.is_long)
        pnl = _partial_pnl(sig.entry_price, sig.is_long, ladder, res.tps_hit, candles[-1].close)
    elif pnl is None and res.exit_price and sig.entry_price > 0:
        pnl = (
            (res.exit_price - sig.entry_price) / sig.entry_price * 100
            if sig.is_long
            else (sig.entry_price - res.exit_price) / sig.entry_price * 100
        )
    if pnl is None:
        return None
    return pnl / risk


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
    rules: tuple[str, ...] = ("breakeven", "ladder", "none"),
    limit: int = 200,
) -> dict:
    """Score each stop-advance rule over the same resolved signals.

    Candles are fetched once per signal and replayed under every rule, so the
    columns cannot drift apart: a signal either scores under all rules or is
    excluded from all of them. Fetching per rule let each one skip a different
    set of signals, and three trades' difference in membership was enough to
    account for the entire gap between them.
    """
    raw = tracker._store.read(OUTCOMES_KEY, default=[]) or []
    from wolf.models import Signal

    signals = [Signal.from_dict(d) for d in raw if isinstance(d, dict)][-limit:]
    signals = [s for s in signals if Status(s.status).is_graded and s.tp_ladder]
    if not signals:
        return {"error": "no graded signals with a ladder to replay", "results": []}

    histories = []
    for sig in signals:
        try:
            candles = _history(tracker, sig)
        except Exception:
            log.exception("History fetch failed for %s", sig.symbol)
            candles = None
        if candles:
            histories.append((sig, candles))
    if not histories:
        return {"error": "no signal had price history reaching back to its entry",
                "results": []}

    probes = {
        rule: Tracker(tracker._store, tracker._client, tracker._settings,
                      ladder=replace(tracker._ladder, stop_advance=rule))
        for rule in rules
    }
    scored: dict[str, list[float]] = {rule: [] for rule in rules}
    for sig, candles in histories:
        row = {rule: _regrade_one(probes[rule], sig, candles) for rule in rules}
        if any(v is None for v in row.values()):
            continue  # scored under every rule, or under none
        for rule, value in row.items():
            scored[rule].append(value)

    n_scored = len(next(iter(scored.values()))) if scored else 0
    if not n_scored:
        return {"error": "no signal could be re-graded under every rule", "results": []}
    return {
        "error": "",
        "sample": len(signals),
        "scored": n_scored,
        "skipped": len(signals) - n_scored,
        "results": [_summarise(rule, scored[rule]) for rule in rules],
        "paired": [_paired(rules[0], rule, scored[rules[0]], scored[rule])
                   for rule in rules[1:]],
    }


def _paired(base: str, rule: str, a: list[float], b: list[float]) -> dict:
    """Compare two rules trade by trade, which is what the design supports.

    Every trade is scored under both rules, so the two columns are not
    independent samples and comparing their means throws away the pairing.
    Most differences are exactly zero — the rules only diverge on trades that
    reached a rung past the first — and it is the handful that moved, and by
    how much, that decides whether a gap is worth acting on. A mean built from
    six changed trades out of fifty-eight is a different claim to one built
    from fifty-eight.
    """
    diffs = [y - x for x, y in zip(a, b)]
    changed = [d for d in diffs if abs(d) > 1e-9]
    mean = statistics.fmean(diffs) if diffs else 0.0
    sd = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
    se = sd / math.sqrt(len(diffs)) if sd else 0.0
    return {
        "base": base,
        "rule": rule,
        "changed": len(changed),
        "helped": sum(1 for d in changed if d > 0),
        "hurt": sum(1 for d in changed if d < 0),
        "mean_diff": round(mean, 4),
        "t": round(mean / se, 2) if se else 0.0,
    }


def render(report: dict) -> str:
    if report.get("error"):
        return f"WHATIF stop rules: {report['error']}"
    lines = [
        f"WHATIF stop rules | scored {report['scored']} of {report['sample']} "
        f"resolved signals ({report['skipped']} without usable history)"
    ]
    lines += [r.line() for r in report["results"]]
    for p in report.get("paired", []):
        lines.append(
            f"vs {p['base']:<9} {p['rule']}: changed {p['changed']} trade(s) "
            f"(+{p['helped']}/-{p['hurt']}) diff={p['mean_diff']:+.4f}R t={p['t']:+.2f}"
        )
    # Measured against the rule in force, not against the worst of the set.
    # Best-minus-worst credited "ladder" with +0.130R by subtracting "none",
    # a rule nobody is running, when the change actually on offer was +0.015R.
    results = report["results"]
    base = results[0]
    best = max(results, key=lambda r: r.mean_r)
    gain = best.mean_r - base.mean_r
    lines.append(
        f"=> {best.rule} beats the live rule ({base.rule}) by {gain:+.3f}R/trade"
        if gain > 0 else f"=> nothing beats the live rule ({base.rule})"
    )
    return "\n".join(lines)


# ── cost gate ───────────────────────────────────────────────────────────────
#: Thresholds worth pricing. 0.5 is the shipped default; the rest bracket it.
COST_GATE_STEPS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 999.0)


@dataclass
class GateResult:
    """What one max_cost_r would have kept, and what that sample returned."""

    threshold: float
    kept: int
    share: float
    mean_r: float
    cost_r: float
    net_r: float

    def line(self) -> str:
        label = "no gate" if self.threshold >= 999 else f"{self.threshold:.2f}R"
        return (
            f"{label:>8}  kept={self.kept:<4} ({self.share:>4.0f}%)  "
            f"meanR={self.mean_r:+.3f}  cost={self.cost_r:.3f}  netR={self.net_r:+.3f}"
        )


def compare_cost_gates(
    tracker: Tracker,
    round_trip_bps: float = 20.0,
    window_hours: Optional[float] = None,
) -> dict:
    """Price the max_cost_r gate against signals already resolved.

    Needs no price history: the stop distance and the realised R are both on
    the record, and the gate is a function of the stop distance alone. So this
    can use every stored outcome rather than the recent slice a replay reaches.

    Read the whole curve, not the best row. The threshold is being chosen on
    the same trades it is scored against, so the best row is partly a fit to
    this sample and will flatter itself. What the curve can honestly show is
    shape: a net figure that climbs steadily as the gate tightens is telling
    you something a single winning row is not.
    """
    from wolf.models import Signal

    raw = tracker._store.read(OUTCOMES_KEY, default=[]) or []
    signals = [Signal.from_dict(d) for d in raw if isinstance(d, dict)]
    signals = [s for s in signals if Status(s.status).is_graded]
    if window_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        signals = [s for s in signals if (_resolved_at(s) or cutoff) >= cutoff]

    # Outcomes booked before the scaled-exit fix carry an R the ladder cannot
    # pay. Dropping them one by one is worse than keeping them: only the full
    # runs were inflated, so filtering on the value strips that era's biggest
    # winners while leaving every one of its losses in place, and the sample
    # comes back negative by construction.
    #
    # The era has to go as a whole, and it dates itself — the newest impossible
    # outcome is the last one booked before the fix landed, so everything
    # settled by then is suspect and everything after it is clean.
    ceiling = tracker._ladder.full_run_r
    cutoff = None
    for sig in signals:
        if r_multiple_of(sig) > ceiling + 1e-9:
            at = _resolved_at(sig)
            if at and (cutoff is None or at > cutoff):
                cutoff = at
    dropped = 0
    rows = []
    for sig in signals:
        at = _resolved_at(sig)
        if cutoff is not None and at is not None and at <= cutoff:
            dropped += 1
            continue
        risk = _risk_pct(sig)
        if not risk:
            continue
        rows.append((r_multiple_of(sig), (round_trip_bps / 100.0) / risk))
    if not rows:
        return {"error": "no resolved signals after the accounting fix", "results": []}
    inflated = dropped

    total = len(rows)
    results = []
    for threshold in COST_GATE_STEPS:
        kept = [(r, c) for r, c in rows if c <= threshold]
        if not kept:
            continue
        mean_r = statistics.fmean(r for r, _ in kept)
        cost_r = statistics.fmean(c for _, c in kept)
        results.append(GateResult(
            threshold=threshold,
            kept=len(kept),
            share=round(len(kept) / total * 100, 1),
            mean_r=round(mean_r, 3),
            cost_r=round(cost_r, 3),
            net_r=round(mean_r - cost_r, 3),
        ))
    return {"error": "", "sample": total, "excluded_inflated": inflated,
            "cutoff": cutoff.isoformat(timespec="minutes") if cutoff else "",
            "results": results}


def render_cost_gates(report: dict) -> str:
    if report.get("error"):
        return f"WHATIF cost gate: {report['error']}"
    lines = [f"WHATIF cost gate | {report['sample']} resolved signals"]
    if report["excluded_inflated"]:
        lines.append(
            f"  ({report['excluded_inflated']} outcomes dropped: settled on or "
            f"before {report['cutoff']}, when booking was still wrong)"
        )
    lines += [r.line() for r in report["results"]]
    lines.append("  the threshold is picked on the trades it is scored against;")
    lines.append("  read the shape of the curve, not the winning row.")
    return "\n".join(lines)
