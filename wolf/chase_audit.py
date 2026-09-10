"""Grade what the chase gate threw away.

``Screener._reprice_at_market`` drops a market entry that has already run
``max_chase_r`` past the price its detector quoted. The drop happens before
``record_signal``, so the candidate never becomes an outcome: it appears in no
count, no bucket and no strategy row on the diagnostic. That is the project's
signature fault — a component deciding what does not happen, and thereby
deciding what cannot be measured — and it is why the drops are now persisted.

Persisting them made the gate *visible*. It did not make it *judged*. A count
says how often the gate fires; it says nothing about whether the candidates it
rejected would have paid, and without that the limit can only ever be argued
from replayed candle shapes, which is how 1.5R was picked and then withdrawn.

This module closes the loop. Each recorded drop is reconstituted as the signal
the screener would have written had the gate not fired, and replayed through
the *same* evaluator that grades real trades, over the candles that actually
followed. Nothing here is a backtest: it invents no entries and re-uses only
setups the bot itself produced and then discarded.

What a reconstituted drop is made of
------------------------------------
The record carries the quote, the live price, and the stop. That is enough,
because a re-quote only ever moves one of the three:

* **entry** becomes ``live`` — the price ``_reprice_at_market`` would have
  written, since the drop is decided *after* the live price is read.
* **stop** stays where the detector put it. It marks a level in the market, not
  an offset from a quote, which is the whole reason chasing stretches the risk
  unit.
* **ladder** is rebuilt from that stretched risk unit at the R multiples the
  policy sets, exactly as the re-quote path rebuilds it. The rungs are not
  recorded and do not need to be: the ladder is threaded from one
  ``LadderSettings`` into every detector precisely so it cannot drift.

``entry_quoted_live`` is set, which is not cosmetic — it tells the replay the
entry was priced at the drop timestamp rather than at some earlier bar close,
so the window between the two is neither credited to the entry nor hidden from
the stop.

What the answer is worth
------------------------
Less than a paired one. The drops and the trades the bot took are different
setups, so comparing them is a *level* question and carries a level question's
price: the gap is Welch's, charged the same overlap discount as everything
else, and it will read INCONCLUSIVE for a long time. The within-population
split by chase distance is the cheaper half — if drops that ran 1.4R past the
quote return what drops that ran 0.6R do, the limit's exact value is not what
matters, and if they do not, the drops themselves say where to put it.

Unresolved rows are reported separately and never folded in silently. A drop
whose history ran out is marked to the last close, which is a guess about a
position that was never closed, and the gate's own effect is largest on
exactly the fast moves most likely to still be open.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from wolf.config import LadderSettings
from wolf.detectors.base import DEFAULT_LADDER, ladder_from_risk
from wolf.models import EntryMode, Signal, Status
from wolf.stats import mean_gap, t_to_p
from wolf.tracker import OUTCOMES_KEY, Tracker, _parse_iso, r_multiple_of
from wolf.whatif import _history, _replay_one

log = logging.getLogger("wolf.chase_audit")

#: Where the screener persists them. Duplicated as a constant rather than
#: imported so this module does not drag the screener (and its exchange client)
#: into the diagnostic path.
CHASE_DROPS_KEY = "chase_drops"

#: Strategy names whose ``signal_type`` differs from them. Only needed for rows
#: written before the type was recorded; newer ones carry their own.
_LEGACY_SIGNAL_TYPE = {"MOMENTUM": "SCREENER"}


def _as_signal(row: dict, ladder_cfg: LadderSettings) -> Optional[Signal]:
    """Rebuild the signal this drop would have been, or ``None`` if it cannot.

    Returns ``None`` rather than guessing whenever the record is too thin to
    reconstitute honestly — a missing stop, an inverted geometry, a ladder that
    will not place. A drop that cannot be graded is not evidence either way.
    """
    try:
        symbol = str(row["symbol"])
        direction = str(row["direction"]).upper()
        entry = float(row["live"])
        sl = float(row["sl"])
        at = str(row["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if entry <= 0 or sl <= 0:
        return None

    is_long = direction == "LONG"
    risk = (entry - sl) if is_long else (sl - entry)
    if risk <= 0:
        return None  # the stop was already through: nothing to grade

    rungs = ladder_from_risk(entry, risk, is_long, ladder_cfg)
    if not rungs:
        return None

    strategy = str(row.get("strategy") or "")
    signal_type = str(
        row.get("signal_type") or _LEGACY_SIGNAL_TYPE.get(strategy, strategy) or "SCREENER"
    )
    return Signal(
        symbol=symbol,
        signal_type=signal_type,
        direction=direction,
        entry_price=entry,
        tp=rungs[-1]["price"],
        sl=sl,
        score=int(row.get("score") or 0),
        strategy=strategy,
        timeframe=str(row.get("timeframe") or "15m"),
        entry_mode=EntryMode.MOMENTUM_NOW.value,
        tp_ladder=rungs,
        created_at=at,
        # The entry is the live price read at this instant, not a bar close, so
        # the replay must open strictly after it.
        entry_quoted_live=True,
    )


def _chase_of(row: dict) -> Optional[float]:
    try:
        return float(row["chase_r"])
    except (KeyError, TypeError, ValueError):
        return None


def _summarise(label: str, rows: list[dict]) -> dict:
    rs = [r["r"] for r in rows]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    chases = [r["chase_r"] for r in rows if r["chase_r"] is not None]
    return {
        "label": label,
        "n": len(rs),
        "resolved": sum(1 for r in rows if r["resolved"]),
        "mean_r": round(statistics.fmean(rs), 3) if rs else 0.0,
        "total_r": round(sum(rs), 2),
        "win_rate": round(len(wins) / len(rs) * 100, 1) if rs else 0.0,
        "avg_win_r": round(statistics.fmean(wins), 3) if wins else 0.0,
        "avg_loss_r": round(statistics.fmean(losses), 3) if losses else 0.0,
        "median_chase_r": round(statistics.median(chases), 2) if chases else None,
    }


def _taken_rs(tracker: Tracker, strategies: set[str], since: datetime) -> list[float]:
    """R-multiples of the trades the bot actually took, for the comparison.

    Restricted to the strategies that were dropped and to the same stretch of
    time, because the gate only ever fired on those: comparing a SCALP drop
    against a week of SWING would answer a question nobody asked.
    """
    raw = tracker._store.read(OUTCOMES_KEY, default=[]) or []
    out: list[float] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            sig = Signal.from_dict(d)
        except (TypeError, ValueError):
            continue
        if sig.strategy not in strategies or not Status(sig.status).is_graded:
            continue
        stamp = sig.resolved_at or sig.created_at
        try:
            if _parse_iso(stamp) < since:
                continue
        except (TypeError, ValueError):
            continue
        out.append(r_multiple_of(sig))
    return out


def grade_chase_drops(
    tracker: Tracker,
    ladder: LadderSettings = DEFAULT_LADDER,
    limit: int = 200,
    overlap: float = 1.0,
) -> dict:
    """Replay every recorded chase drop and report what it would have returned.

    ``overlap`` is the mean concurrent-position count the diagnostic reports.
    It is passed in rather than recomputed because the drops were never open
    positions — they have no concurrency of their own — so the honest discount
    to charge them is the one the live book was running at the time.
    """
    rows = tracker._store.read(CHASE_DROPS_KEY, default=[]) or []
    if not isinstance(rows, list) or not rows:
        return {"error": "no chase drops recorded yet", "sample": 0, "scored": []}
    rows = rows[-limit:]

    scored: list[dict] = []
    skipped_unbuildable = 0
    skipped_nohistory = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped_unbuildable += 1
            continue
        sig = _as_signal(row, ladder)
        if sig is None:
            skipped_unbuildable += 1
            continue
        try:
            candles = _history(tracker, sig)
        except Exception:
            log.exception("History fetch failed for chase drop on %s", sig.symbol)
            candles = None
        if not candles:
            skipped_nohistory += 1
            continue
        replay = _replay_one(tracker, sig, candles)
        if replay is None:
            skipped_nohistory += 1
            continue
        scored.append({
            "symbol": sig.symbol,
            "strategy": sig.strategy,
            "chase_r": _chase_of(row),
            "r": replay.r,
            "resolved": replay.resolved,
        })

    if not scored:
        return {
            "error": "no recorded drop could be replayed — history did not reach back",
            "sample": len(rows),
            "skipped_unbuildable": skipped_unbuildable,
            "skipped_nohistory": skipped_nohistory,
            "scored": [],
        }

    by_strategy = {}
    for name in sorted({s["strategy"] for s in scored}):
        by_strategy[name] = _summarise(name, [s for s in scored if s["strategy"] == name])

    # Within-population split on how far past the quote each drop had run. This
    # is the half that can argue the limit without a second sample: the median
    # is used rather than the configured limit because every recorded drop sits
    # above that limit by definition, so it would put everything in one cell.
    by_distance = {}
    chases = [s["chase_r"] for s in scored if s["chase_r"] is not None]
    if len(chases) >= 4:
        cut = statistics.median(chases)
        near = [s for s in scored if s["chase_r"] is not None and s["chase_r"] <= cut]
        far = [s for s in scored if s["chase_r"] is not None and s["chase_r"] > cut]
        if near and far:
            by_distance = {
                "cut": round(cut, 2),
                "near": _summarise(f"<={cut:.2f}R", near),
                "far": _summarise(f">{cut:.2f}R", far),
            }

    # The level comparison, and priced as one. Anchored to the oldest drop so
    # the two populations cover the same stretch of market.
    oldest = min(
        (_parse_iso(str(r.get("at"))) for r in rows if isinstance(r, dict) and r.get("at")),
        default=datetime.now(timezone.utc) - timedelta(days=7),
    )
    taken = _taken_rs(tracker, {s["strategy"] for s in scored}, oldest)
    gap = mean_gap([s["r"] for s in scored], taken, overlap=overlap) if taken else None

    return {
        "error": "",
        "sample": len(rows),
        "skipped_unbuildable": skipped_unbuildable,
        "skipped_nohistory": skipped_nohistory,
        "overall": _summarise("all", scored),
        "by_strategy": by_strategy,
        "by_distance": by_distance,
        "taken_n": len(taken),
        "taken_mean_r": round(statistics.fmean(taken), 3) if taken else None,
        "gap": gap,
        "overlap": overlap,
        "scored": scored,
    }


def _line(row: dict) -> str:
    unresolved = row["n"] - row["resolved"]
    tail = f" ({unresolved} still open, marked to last close)" if unresolved else ""
    chase = f" chase~{row['median_chase_r']:.2f}R" if row["median_chase_r"] is not None else ""
    return (
        f"{row['label']:<10} n={row['n']} meanR={row['mean_r']:+.3f} "
        f"totalR={row['total_r']:+.2f} wr={row['win_rate']:.1f}"
        f" avgWin={row['avg_win_r']:+.2f}R avgLoss=-{row['avg_loss_r']:.2f}R{chase}{tail}"
    )


def render(report: dict) -> str:
    """Render the audit as a compact block, in the diagnostic's own idiom."""
    if report.get("error"):
        return f"CHASE-AUDIT | {report['error']}"

    lines = [
        f"CHASE-AUDIT | {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"| drops={report['sample']} replayed={report['overall']['n']}",
        _line(report["overall"]),
    ]
    skipped = report.get("skipped_unbuildable", 0) + report.get("skipped_nohistory", 0)
    if skipped:
        lines.append(
            f"{'skipped':<10} {skipped} — {report.get('skipped_nohistory', 0)} had no history "
            f"reaching back, {report.get('skipped_unbuildable', 0)} could not be rebuilt"
        )
    for row in report.get("by_strategy", {}).values():
        lines.append(_line(row))

    dist = report.get("by_distance") or {}
    if dist:
        lines.append(f"{'distance':<10} split at the median drop, {dist['cut']:.2f}R past the quote")
        lines.append("  " + _line(dist["near"]))
        lines.append("  " + _line(dist["far"]))

    gap = report.get("gap")
    if gap:
        p = t_to_p(gap["t"], gap["df"])
        lines.append(
            f"{'vs taken':<10} drops meanR={report['overall']['mean_r']:+.3f} "
            f"vs taken {report['taken_mean_r']:+.3f} (n={report['taken_n']}) "
            f"=> gap={gap['gap_r']:+.3f}R t={gap['t']:+.2f} "
            f"(nom {gap['t_nominal']:+.2f}) df={gap['df']} p={p:.3f} eff={gap['eff_n']}"
        )
        lines.append(
            f"{'':<10} unpaired — different setups, so this is a level question "
            f"charged mean_open={report['overlap']:.2f}; expect it to say nothing for a while"
        )
    lines.append(
        f"{'note':<10} a drop is replayed as the signal the re-quote would have "
        f"written: entry at the live price, stop unmoved, ladder rebuilt on the "
        f"stretched risk unit"
    )
    return "\n".join(lines)
