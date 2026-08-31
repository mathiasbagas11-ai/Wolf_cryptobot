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


@dataclass
class Replay:
    """One signal's replayed outcome, and whether the history settled it.

    ``resolved`` matters because the two are not equally trustworthy. A trade
    the candles carried to a rung or a stop is a measurement; one still open
    when the history ran out is marked to the last close, which is a guess
    about a position that was never closed. Comparing geometries makes the
    distinction load-bearing: a wider ladder takes longer to fill, so it
    systematically leaves more trades unsettled, and a variant can look better
    purely because more of its trades were valued mid-move instead of at a
    stop. The count travels with the result so the reader can see it.
    """

    r: float
    resolved: bool


def _replay_one(probe: Tracker, sig, candles: list) -> Optional[Replay]:
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
    resolved = True
    if pnl is None and res.terminal is None:
        resolved = False
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
    return Replay(r=pnl / risk, resolved=resolved)


def _regrade_one(probe: Tracker, sig, candles: list) -> Optional[float]:
    """The replayed R alone, for callers that do not track settlement."""
    replay = _replay_one(probe, sig, candles)
    return None if replay is None else replay.r


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


# ── ladder geometry ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LadderVariant:
    """One target geometry, expressed the way the live config expresses it.

    The stop is not part of this. A detector chooses where the stop sits, that
    distance is 1R, and every rung is placed at a multiple of it — so varying
    the geometry moves the targets while leaving the entry, the direction and
    the risk unit exactly as the bot actually took them. That is what makes the
    columns comparable: the denominator of every R is identical across
    variants, and no variant invents a trade the other did not take.
    """

    label: str
    rr_target: float
    fractions: tuple[float, ...]
    allocations: tuple[float, ...]

    def settings(self, live: LadderSettings) -> LadderSettings:
        """This geometry, carrying the live stop and intrabar conventions.

        Only the targets are under test. Inheriting ``stop_advance`` keeps the
        comparison about where the rungs go rather than silently re-running the
        stop-rule question that ``compare_stop_rules`` already answers.
        """
        return replace(
            live,
            rr_target=self.rr_target,
            tp_ladder_fractions=self.fractions,
            tp_allocations=self.allocations,
        )

    def rungs_for(self, sig) -> list[dict]:
        """The ladder this geometry would have placed on ``sig``."""
        risk = abs(sig.entry_price - sig.sl)
        rungs = []
        for level, (fraction, allocation) in enumerate(
            zip(self.fractions, self.allocations), start=1
        ):
            r = self.rr_target * fraction
            offset = r * risk
            rungs.append({
                "level": level,
                "price": sig.entry_price + offset if sig.is_long
                else sig.entry_price - offset,
                "allocation": allocation,
                "r_multiple": r,
            })
        return rungs


#: The geometries worth pricing, spanning both directions out of the 1:1 that
#: the ladder actually realises. ``live`` first, so every paired comparison is
#: measured against the rule in force rather than the worst of the set.
#:
#: Two rival explanations for "TP3 never fills", and they imply opposite fixes:
#: the ladder reaches too far (pull it in — ``rr2.5``, ``rr2.0``, ``2rung``),
#: or too much size comes off at the near rung to leave a runner worth having
#: (move it back — ``backload``, ``even``). Guessing between them is what the
#: replay exists to avoid, so both are on the card.
LADDER_VARIANTS: tuple[LadderVariant, ...] = (
    LadderVariant("live", 3.0, (1 / 3, 2 / 3, 1.0), (0.5, 0.3, 0.2)),
    LadderVariant("rr2.5", 2.5, (1 / 3, 2 / 3, 1.0), (0.5, 0.3, 0.2)),
    LadderVariant("rr2.0", 2.0, (1 / 3, 2 / 3, 1.0), (0.5, 0.3, 0.2)),
    LadderVariant("2rung", 2.0, (0.5, 1.0), (0.5, 0.5)),
    LadderVariant("backload", 3.0, (1 / 3, 2 / 3, 1.0), (0.3, 0.3, 0.4)),
    LadderVariant("even", 3.0, (1 / 3, 2 / 3, 1.0), (1 / 3, 1 / 3, 1 / 3)),
)


@dataclass
class GeometryResult:
    """What one geometry would have returned, and how settled that sample was."""

    label: str
    full_run_r: float
    n: int
    unresolved: int
    mean_r: float
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    breakeven_wr: float

    def line(self) -> str:
        return (
            f"{self.label:<9} run={self.full_run_r:.2f}R meanR={self.mean_r:+.3f} "
            f"wr={self.win_rate:>4.1f} need={self.breakeven_wr:>4.1f} "
            f"aW={self.avg_win_r:+.2f} aL=-{self.avg_loss_r:.2f} open={self.unresolved}"
        )


def _summarise_geometry(
    variant: LadderVariant, live: LadderSettings, rs: list[float], unresolved: int
) -> GeometryResult:
    """Score one geometry, including the win rate it would have to earn.

    ``breakeven_wr`` is derived from the wins and losses this geometry actually
    produced, not from its advertised ratio. That is the whole point of the
    exercise: a 1:3 ladder that banks half at the near rung does not need the
    win rate a 1:3 ladder implies, and quoting the advertised number is how a
    system comes to be sold at 1:3 while realising 1:1.
    """
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    return GeometryResult(
        label=variant.label,
        full_run_r=round(variant.settings(live).full_run_r, 2),
        n=len(rs),
        unresolved=unresolved,
        mean_r=round(statistics.fmean(rs), 3) if rs else 0.0,
        win_rate=round(len(wins) / len(rs) * 100, 1) if rs else 0.0,
        avg_win_r=round(avg_win, 3),
        avg_loss_r=round(avg_loss, 3),
        breakeven_wr=round(avg_loss / (avg_win + avg_loss) * 100, 1)
        if (avg_win + avg_loss) else 0.0,
    )


def compare_ladder_geometry(
    tracker: Tracker,
    variants: tuple[LadderVariant, ...] = LADDER_VARIANTS,
    limit: int = 200,
) -> dict:
    """Re-cut the target ladder on already-resolved signals and re-grade them.

    Answers the question the aggregate card raises and cannot settle: the
    system is sold at 1:3 and realises about 1:1, the far rung almost never
    fills, and it is not obvious whether the fix is a nearer ladder or a
    heavier runner. Both are re-cut here on the same entries and the same
    candles, so the answer comes from the trades rather than from an argument.

    Two things this deliberately does not do. It does not move the stop, so
    every variant carries the identical risk unit and the R columns compare
    directly. And it does not invent entries — the detector fired where it
    fired, and only the targets move.

    The honest limit is settlement. A ladder reaching further takes longer to
    fill, the fetched history is finite, and a trade still open at the end is
    valued at the last close. That favours the wider geometries, so every row
    reports how many of its trades were unsettled and the reader is told to
    distrust a winner that carries many.
    """
    raw = tracker._store.read(OUTCOMES_KEY, default=[]) or []
    from wolf.models import Signal

    signals = [Signal.from_dict(d) for d in raw if isinstance(d, dict)][-limit:]
    signals = [s for s in signals if Status(s.status).is_graded and s.tp_ladder]
    signals = [s for s in signals if s.entry_price > 0 and s.sl > 0
               and abs(s.entry_price - s.sl) > 0]
    if not signals:
        return {"error": "no graded signals with a usable risk unit", "results": []}

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

    live = tracker._ladder
    probes = {
        v.label: Tracker(tracker._store, tracker._client, tracker._settings,
                         ladder=v.settings(live))
        for v in variants
    }
    scored: dict[str, list[float]] = {v.label: [] for v in variants}
    unresolved: dict[str, int] = {v.label: 0 for v in variants}
    for sig, candles in histories:
        # Every variant grades the same signal or none of them do, so the
        # columns cannot drift apart on membership — the failure that once
        # accounted for an entire apparent gap between two stop rules.
        row = {}
        for v in variants:
            rungs = v.rungs_for(sig)
            probe_sig = replace(sig, tp_ladder=rungs, tp=rungs[-1]["price"])
            row[v.label] = _replay_one(probes[v.label], probe_sig, candles)
        if any(r is None for r in row.values()):
            continue
        for label, replay in row.items():
            scored[label].append(replay.r)
            if not replay.resolved:
                unresolved[label] += 1

    n_scored = len(scored[variants[0].label])
    if not n_scored:
        return {"error": "no signal could be re-graded under every geometry",
                "results": []}
    base = variants[0].label
    return {
        "error": "",
        "sample": len(signals),
        "scored": n_scored,
        "skipped": len(signals) - n_scored,
        "results": [_summarise_geometry(v, live, scored[v.label], unresolved[v.label])
                    for v in variants],
        "paired": [_paired(base, v.label, scored[base], scored[v.label])
                   for v in variants[1:]],
    }


def render_ladder(report: dict) -> str:
    if report.get("error"):
        return f"WHATIF ladder: {report['error']}"
    lines = [
        f"WHATIF ladder | scored {report['scored']} of {report['sample']} "
        f"resolved signals ({report['skipped']} without usable history)",
        "run=ceiling if every rung fills  need=WR this geometry must earn",
        "open=trades the history never settled (valued at last close)",
    ]
    lines += [r.line() for r in report["results"]]
    for p in report.get("paired", []):
        lines.append(
            f"vs {p['base']:<9} {p['rule']:<9} changed {p['changed']} "
            f"(+{p['helped']}/-{p['hurt']}) diff={p['mean_diff']:+.4f}R t={p['t']:+.2f}"
        )
    results = report["results"]
    base = results[0]
    best = max(results, key=lambda r: r.mean_r)
    gain = best.mean_r - base.mean_r
    lines.append(
        f"=> {best.label} beats the live geometry by {gain:+.3f}R/trade"
        if gain > 0 else "=> nothing beats the live geometry"
    )
    # The settlement caveat attaches to whichever row is being read, and the
    # live column is not exempt: when it wins while holding the most open
    # positions, its own number is the one marked to the last close, and a
    # warning that only fired for challengers would stay silent on exactly the
    # reading most likely to be acted on — "leave it alone".
    if best.unresolved:
        if best.unresolved > base.unresolved:
            lines.append(
                f"   {best.label} left {best.unresolved - base.unresolved} more "
                f"trade(s) unsettled than live, which flatters it — untested."
            )
        else:
            lines.append(
                f"   {best.label} is still holding {best.unresolved} unsettled "
                f"trade(s), valued at the last close, not closed — untested."
            )
    lines.append("  the geometry is picked on the trades it is scored against;")
    lines.append("  read the shape across variants, not the winning row.")
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
