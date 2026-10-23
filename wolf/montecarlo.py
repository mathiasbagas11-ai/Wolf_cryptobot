"""How bad the next month could look if the ledger's past repeats.

The paper balance is one path: the trades happened in one order and that order
produced one drawdown. Dealing the same trades again in other orders shows the
spread of paths the same results could have produced — the losing run to size
against before it arrives rather than after.

Two choices separate this from the textbook version, and both run against it
looking good.

**Days are dealt, not trades.** Shuffling single trades treats every position
as independent, which the ledger has said repeatedly they are not: on 09-28
twelve positions stopped out through one market move, about −15.7% of the
paper balance in a day. A trade shuffle scatters such a day across the month
and reports a gentler drawdown than the bot has already lived through. So the
resampling unit is a whole resolution day, kept intact, and the trade-shuffle
figure is printed beside it only so the size of that understatement is visible
— never so it can be preferred. Same reason the diag card prints ``nom``.

**Costs are charged.** ``PaperAccount.apply`` books gross PnL, so ``/paper``
shows a curve no account could reach. Each trade here pays the round-trip cost
on its own stop distance before it moves the balance; the gross figure is
shown beside it for the gap.

What this cannot say is whether the edge is real. It resamples the measured
mean, whatever it is — a positive mean that is luck yields comfortable
paths. It answers "if the next month draws from these days, how deep could the
hole get", which is a sizing question, and leaves "is there an edge" to the
diag card. The days available are capped by ``MAX_OUTCOMES``, so the pool is
a few weeks at most and says so.
"""

from __future__ import annotations

import random
import statistics
from datetime import datetime
from typing import Iterable, Optional

from wolf.models import Signal, Status
from wolf.tracker import _parse_iso, _risk_pct, r_multiple_of

#: Fewer distinct days than this and the resample is mostly one week repeated.
MIN_DAYS = 7


def _resolved_at(sig: Signal) -> Optional[datetime]:
    for raw in (sig.resolved_at, sig.exit_time):
        if raw:
            try:
                return _parse_iso(raw)
            except (TypeError, ValueError):
                continue
    return None


def _trade_rows(outcomes: Iterable[Signal], round_trip_bps: float,
                strategy: Optional[str]) -> list[dict]:
    """One row per traded outcome: when it hit the balance, gross and net R."""
    rows = []
    for o in outcomes:
        if o.status == Status.INVALIDATED.value or not Status(o.status).is_terminal:
            continue
        if strategy and (o.strategy or "").upper() != strategy:
            continue
        risk = _risk_pct(o)
        ts = _resolved_at(o)
        if not risk or ts is None:
            continue
        gross = r_multiple_of(o)
        cost = (round_trip_bps / 100.0) / risk
        rows.append({
            "at": ts,
            "gross": gross,
            "net": gross - cost,
            "scale": getattr(o, "risk_scale", 1.0) or 1.0,
        })
    rows.sort(key=lambda r: r["at"])
    return rows


def _walk(trades: Iterable[tuple[float, float]], k: float) -> tuple[float, float, int]:
    """Compound ``(R, scale)`` pairs at ``k`` of equity per R.

    Returns ``(final return %, max drawdown %, longest losing streak)``.
    Drawdown is taken trade by trade, so a day that dips and recovers still
    shows its dip.
    """
    eq = peak = 1.0
    max_dd = 0.0
    streak = worst = 0
    for r, scale in trades:
        eq *= max(0.0, 1.0 + k * scale * r)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
        if r < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return (eq - 1.0) * 100, max_dd * 100, worst


def ledger_path(outcomes: Iterable[Signal], *, risk_pct: float = 1.0,
                round_trip_bps: float = 20.0) -> Optional[dict]:
    """The one path the capped log holds, compounded gross and net of cost.

    For ``/paper``: the net balance there only starts on the day it was added,
    and this is what the same arithmetic says about the weeks before it.
    """
    rows = _trade_rows(outcomes, round_trip_bps, None)
    if not rows:
        return None
    k = risk_pct / 100.0
    gross = _walk(((t["gross"], t["scale"]) for t in rows), k)
    net = _walk(((t["net"], t["scale"]) for t in rows), k)
    return {
        "first_day": rows[0]["at"].date().isoformat(),
        "last_day": rows[-1]["at"].date().isoformat(),
        "trades": len(rows),
        "gross_pct": round(gross[0], 1), "net_pct": round(net[0], 1),
        "net_max_dd": round(net[1], 1),
    }


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[i]


def simulate(
    outcomes: Iterable[Signal],
    *,
    risk_pct: float = 1.0,
    round_trip_bps: float = 20.0,
    horizon_days: int = 30,
    n_sims: int = 2000,
    strategy: Optional[str] = None,
    seed: int = 7,
) -> dict:
    """Day-block bootstrap of the ledger, net of costs, at ``risk_pct`` per trade."""
    strategy = strategy.upper() if strategy else None
    rows = _trade_rows(outcomes, round_trip_bps, strategy)
    days: dict[str, list[dict]] = {}
    for r in rows:
        days.setdefault(r["at"].date().isoformat(), []).append(r)

    report: dict = {
        "strategy": strategy, "trades": len(rows), "days": len(days),
        "risk_pct": risk_pct, "round_trip_bps": round_trip_bps,
        "horizon_days": horizon_days, "n_sims": n_sims,
    }
    if len(days) < MIN_DAYS:
        report["error"] = (
            f"only {len(days)} trading day(s) in the ledger — need {MIN_DAYS} "
            "before a resample means anything"
        )
        return report

    k = risk_pct / 100.0
    pool = [[(t["net"], t["scale"]) for t in d] for d in days.values()]
    pool_gross = [[(t["gross"], t["scale"]) for t in d] for d in days.values()]
    flat = [t for d in pool for t in d]
    per_day = len(flat) / len(pool)
    n_trades = max(1, round(per_day * horizon_days))
    rng = random.Random(seed)

    ret, dd, streak, ret_gross, dd_shuffle = [], [], [], [], []
    for _ in range(n_sims):
        picks = [rng.randrange(len(pool)) for _ in range(horizon_days)]
        r, d, s = _walk((t for i in picks for t in pool[i]), k)
        ret.append(r)
        dd.append(d)
        streak.append(s)
        ret_gross.append(_walk((t for i in picks for t in pool_gross[i]), k)[0])
        dd_shuffle.append(_walk((rng.choice(flat) for _ in range(n_trades)), k)[1])

    actual_ret, actual_dd, actual_streak = _walk(
        ((t["net"], t["scale"]) for t in rows), k)

    report.update({
        "trades_per_day": round(per_day, 1),
        "mean_net_r": round(statistics.fmean(t["net"] for t in rows), 3),
        "mean_gross_r": round(statistics.fmean(t["gross"] for t in rows), 3),
        "return": {q: round(_pct(ret, p), 1) for q, p in
                   (("p5", 0.05), ("p50", 0.50), ("p95", 0.95))},
        "return_gross_p50": round(_pct(ret_gross, 0.50), 1),
        "p_loss": round(sum(1 for r in ret if r < 0) / n_sims * 100),
        "max_dd": {q: round(_pct(dd, p), 1) for q, p in
                   (("p50", 0.50), ("p95", 0.95), ("p99", 0.99))},
        "max_dd_trade_shuffle_p95": round(_pct(dd_shuffle, 0.95), 1),
        "losing_streak_p95": int(_pct([float(s) for s in streak], 0.95)),
        "actual": {"return": round(actual_ret, 1), "max_dd": round(actual_dd, 1),
                   "losing_streak": actual_streak,
                   "first_day": min(days), "last_day": max(days)},
        "weeks": _weekly(rows),
    })
    return report


def _weekly(rows: list[dict]) -> list[dict]:
    """Every ISO week in the ledger — all of them, not the ones someone sent."""
    weeks: dict[str, list[dict]] = {}
    for r in rows:
        y, w, _ = r["at"].isocalendar()
        weeks.setdefault(f"{y}-W{w:02d}", []).append(r)
    return [
        {"week": wk, "n": len(ts),
         "gross": round(statistics.fmean(t["gross"] for t in ts), 3),
         "net": round(statistics.fmean(t["net"] for t in ts), 3)}
        for wk, ts in sorted(weeks.items())
    ]


def _shuffle_line(block_p95: float, shuffle_p95: float) -> str:
    """The textbook figure beside ours, read the right way round."""
    if shuffle_p95 < block_p95:
        why = (f"understates it by {block_p95 - shuffle_p95:.1f} points: "
               "losers here arrive together")
    else:
        why = "no same-day clustering penalty visible in this pool"
    return f"           single-trade shuffle says p95 {shuffle_p95:.1f}% — {why}"


def render(report: dict) -> str:
    who = report["strategy"] or "all strategies"
    head = f"MONTE-CARLO | {who} | {report['trades']} trades over {report['days']} days"
    if "error" in report:
        return f"{head}\n{report['error']}"
    r, d, a = report["return"], report["max_dd"], report["actual"]
    lines = [
        head,
        f"pool       {a['first_day']} -> {a['last_day']}, {report['trades_per_day']}/day, "
        f"meanR {report['mean_gross_r']:+.3f} gross / {report['mean_net_r']:+.3f} net "
        f"of {report['round_trip_bps']:g}bps",
        f"dealt      {report['n_sims']} x {report['horizon_days']} days, whole days kept "
        f"together, {report['risk_pct']:g}% risk per trade",
        f"return     p5 {r['p5']:+.1f}%  median {r['p50']:+.1f}%  p95 {r['p95']:+.1f}%  "
        f"(gross median {report['return_gross_p50']:+.1f}%)",
        f"           {report['p_loss']}% of paths end below where they started",
        f"max DD     median {d['p50']:.1f}%  p95 {d['p95']:.1f}%  p99 {d['p99']:.1f}%",
        _shuffle_line(d["p95"], report["max_dd_trade_shuffle_p95"]),
        f"streak     p95 {report['losing_streak_p95']} losers in a row",
        f"lived      net {a['return']:+.1f}%, max DD {a['max_dd']:.1f}%, "
        f"longest streak {a['losing_streak']} — the one path that happened",
        "weeks      every ISO week in the ledger (net of cost)",
    ]
    for w in report["weeks"]:
        lines.append(f"  {w['week']}  n={w['n']:<4} gross {w['gross']:+.3f}  net {w['net']:+.3f}")
    lines.append(
        "note       resamples the measured mean; it sizes the hole, it does not "
        "say the edge is real — read /diag for that"
    )
    return "\n".join(lines)
