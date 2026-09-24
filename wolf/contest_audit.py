"""Grade the candidates that lost the per-symbol contest.

``Screener._best_candidate`` keeps one candidate per symbol by ``max(score)``
and discards the rest. That is the fifth instance of this project's signature
fault — a component deciding what does not happen, and thereby deciding what
cannot be measured — and it is a costlier one than the chase gate, because the
scores it compares are not on a common scale.

Each detector's hard gates guarantee a different floor before any confluence is
read: 0 for PREPUMP and PREDUMP, 20 for SCALP, 22 for TRAP, 55 for SWING, 85
for MOMENTUM. So a contest between MOMENTUM and PREPUMP is settled largely by
how much each detector pays itself for conditions it has already required,
rather than by which read the market supports. MOMENTUM has been the losing
strategy on four consecutive cards while also taking most contested symbols,
and nothing in the ledger could separate those two facts, because the displaced
candidate left no trace.

Why this is the cheap question
------------------------------
The chase audit compares drops against the trades the bot took — different
setups, so a level question, priced as one, and it duly reported a gap of
+0.015R at p=0.976 after its sampling bias was fixed. This one is **paired**:
the winner and the loser are the same symbol on the same bar, so the market
move cancels. The standing lesson is that paired questions resolve at a
fraction of the sample a level question needs, and this is the last large
decision in the pipeline that was not being asked as one.

Grading follows the chase audit's discipline exactly, for the same reason: the
replay needs 15m candles reaching back to the contest, the bars required grow
every hour, so a loser left ungraded long enough becomes ungradeable on a
criterion correlated with age. An hourly job grades each loser once, close to
the event, and the verdict is stored.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone
from typing import Optional

from wolf.config import LadderSettings
from wolf.detectors.base import DEFAULT_LADDER
from wolf.models import EntryMode, Signal, Status
from wolf.stats import t_to_p
from wolf.tracker import OUTCOMES_KEY, Tracker, _parse_iso, r_multiple_of
from wolf.whatif import _history, _replay_one

log = logging.getLogger("wolf.contest_audit")

#: Mirrors ``screener.CONTESTS_KEY``. Duplicated rather than imported so the
#: diagnostic path does not drag the screener and its exchange client along.
CONTESTS_KEY = "score_contests"


def _loser_signal(row: dict, spec: dict) -> Optional[Signal]:
    """Rebuild a displaced candidate as the signal it would have been.

    Its own detector chose the entry, the stop and the ladder, so all three are
    copied rather than re-derived: rebuilding them from a policy would grade a
    trade that detector never proposed.
    """
    try:
        symbol = str(row["symbol"])
        at = str(row["at"])
        entry = float(spec["entry_price"])
        sl = float(spec["sl"])
        direction = str(spec["direction"]).upper()
    except (KeyError, TypeError, ValueError):
        return None
    if entry <= 0 or sl <= 0:
        return None
    is_long = direction == "LONG"
    if (is_long and sl >= entry) or (not is_long and sl <= entry):
        return None

    rungs = spec.get("tps") or []
    if not rungs:
        return None
    return Signal(
        symbol=symbol,
        signal_type=str(spec.get("signal_type") or "SCREENER"),
        direction=direction,
        entry_price=entry,
        tp=float(spec.get("tp") or rungs[-1]["price"]),
        sl=sl,
        score=int(spec.get("score") or 0),
        strategy=str(spec.get("strategy") or ""),
        timeframe=str(spec.get("timeframe") or "1h"),
        entry_mode=str(spec.get("entry_mode") or EntryMode.MOMENTUM_NOW.value),
        tp_ladder=list(rungs),
        created_at=at,
        # The contest is decided at the moment the winner was quoted, so the
        # loser's replay opens there too — not at some earlier bar close.
        entry_quoted_live=True,
    )


def _age_hours(row: dict, now: datetime) -> Optional[float]:
    try:
        return (now - _parse_iso(str(row["at"]))).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return None


def _is_gradeable(row: dict, spec: dict, settings, now: datetime) -> bool:
    if spec.get("graded"):
        return False
    age = _age_hours(row, now)
    if age is None:
        return False
    ceiling = getattr(settings, "chase_grade_max_age_h", 60)
    floor = min(settings.timeout_for(str(spec.get("signal_type") or "SCREENER")), ceiling)
    return floor <= age <= ceiling


def grade_pending_contests(
    tracker: Tracker, ladder: LadderSettings = DEFAULT_LADDER
) -> dict:
    """Grade every displaced candidate that has come of age, storing verdicts.

    Runs on the scheduler, once per loser, for the same reason the chase grader
    does: the window in which a replay can reach back closes with time.
    """
    now = datetime.now(timezone.utc)
    rows = tracker._store.read(CONTESTS_KEY, default=[]) or []
    if not isinstance(rows, list) or not rows:
        return {"considered": 0, "graded": 0, "failed": 0, "expired": 0}

    verdicts: dict[tuple[str, int], dict] = {}
    considered = failed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for i, spec in enumerate(row.get("losers") or []):
            if not isinstance(spec, dict) or not _is_gradeable(row, spec, tracker._settings, now):
                continue
            considered += 1
            key = (str(row.get("signal_id") or row.get("at")), i)
            sig = _loser_signal(row, spec)
            if sig is None:
                verdicts[key] = {"error": "unbuildable",
                                 "at": now.isoformat(timespec="seconds")}
                continue
            try:
                candles = _history(tracker, sig)
            except Exception:
                log.exception("History fetch failed grading a displaced %s", sig.strategy)
                candles = None
            replay = _replay_one(tracker, sig, candles) if candles else None
            if replay is None:
                failed += 1  # transient: retry next run
                continue
            verdicts[key] = {
                "r": round(replay.r, 4),
                "resolved": replay.resolved,
                "at": now.isoformat(timespec="seconds"),
            }

    if verdicts:
        def _apply(current):
            out = []
            for row in (current or []):
                if not isinstance(row, dict):
                    out.append(row)
                    continue
                losers = []
                for i, spec in enumerate(row.get("losers") or []):
                    key = (str(row.get("signal_id") or row.get("at")), i)
                    verdict = verdicts.get(key)
                    if verdict and isinstance(spec, dict) and not spec.get("graded"):
                        spec = {**spec, "graded": verdict}
                    losers.append(spec)
                out.append({**row, "losers": losers})
            return out

        tracker._store.update(CONTESTS_KEY, _apply, default=[])

    ceiling = getattr(tracker._settings, "chase_grade_max_age_h", 60)
    expired = sum(
        1 for row in rows if isinstance(row, dict) and (_age_hours(row, now) or 0) > ceiling
        for spec in (row.get("losers") or []) if isinstance(spec, dict) and not spec.get("graded")
    )
    if verdicts or failed or expired:
        log.info(
            "Contest grading: %d considered, %d graded, %d deferred, %d aged out",
            considered, len(verdicts), failed, expired,
        )
    return {"considered": considered, "graded": len(verdicts),
            "failed": failed, "expired": expired}


def _outcomes_by_id(tracker: Tracker) -> dict[str, Signal]:
    raw = tracker._store.read(OUTCOMES_KEY, default=[]) or []
    out: dict[str, Signal] = {}
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            sig = Signal.from_dict(d)
        except (TypeError, ValueError):
            continue
        if sig.id:
            out[sig.id] = sig
    return out


def _paired_stats(pairs: list[dict], overlap: float) -> Optional[dict]:
    """Student's t on the per-contest difference, charged the overlap discount.

    Paired rather than two-sample because both sides are the same symbol on the
    same bar: the market move is common to them and cancels in the difference,
    which is the entire reason this question is affordable when the level one
    is not.
    """
    diffs = [p["winner_r"] - p["loser_r"] for p in pairs]
    n = len(diffs)
    if n < 2:
        return None
    sd = statistics.stdev(diffs)
    mean = statistics.fmean(diffs)
    if sd <= 0:
        return None
    eff_n = max(1.0, n / max(overlap, 1.0))
    se = sd / (eff_n ** 0.5)
    se_nominal = sd / (n ** 0.5)
    df = max(1, int(eff_n) - 1)
    return {
        "n": n,
        "eff_n": int(eff_n),
        "mean_diff": round(mean, 3),
        "sd": round(sd, 3),
        "se": round(se, 3),
        "t": round(mean / se, 2),
        "t_nominal": round(mean / se_nominal, 2),
        "df": df,
        "p": round(t_to_p(mean / se, df), 3),
        "winner_better": sum(1 for d in diffs if d > 0),
    }


def audit_contests(tracker: Tracker, limit: int = 300, overlap: float = 1.0) -> dict:
    """Pair each graded loser against the winner that displaced it."""
    rows = tracker._store.read(CONTESTS_KEY, default=[]) or []
    if not isinstance(rows, list) or not rows:
        return {"error": "no score contests recorded yet", "pairs": []}
    rows = rows[-limit:]
    outcomes = _outcomes_by_id(tracker)

    pairs: list[dict] = []
    skipped = {"winner_unresolved": 0, "loser_ungraded": 0, "unbuildable": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        winner = outcomes.get(str(row.get("signal_id") or ""))
        winner_graded = winner is not None and Status(winner.status).is_graded
        for spec in (row.get("losers") or []):
            if not isinstance(spec, dict):
                continue
            verdict = spec.get("graded")
            if not isinstance(verdict, dict):
                skipped["loser_ungraded"] += 1
                continue
            if verdict.get("error"):
                skipped["unbuildable"] += 1
                continue
            if not winner_graded:
                # The winner has not resolved, so there is nothing to pair
                # against. Comparing a settled loser to an open winner would
                # grade the two on different amounts of information.
                skipped["winner_unresolved"] += 1
                continue
            pairs.append({
                "symbol": str(row.get("symbol") or ""),
                "winner": str((row.get("winner") or {}).get("strategy") or ""),
                "winner_score": int((row.get("winner") or {}).get("score") or 0),
                "winner_r": r_multiple_of(winner),
                "loser": str(spec.get("strategy") or ""),
                "loser_score": int(spec.get("score") or 0),
                "loser_r": float(verdict["r"]),
                "loser_resolved": bool(verdict.get("resolved")),
            })

    if not pairs:
        return {"error": "no contest has both a resolved winner and a graded loser yet",
                "sample": len(rows), "skipped": skipped, "pairs": []}

    by_matchup: dict[str, dict] = {}
    for p in pairs:
        key = f"{p['winner']}>{p['loser']}"
        by_matchup.setdefault(key, []).append(p)

    return {
        "error": "",
        "sample": len(rows),
        "skipped": skipped,
        "overlap": overlap,
        "overall": _paired_stats(pairs, overlap),
        "by_matchup": {
            k: {"n": len(v),
                "mean_diff": round(statistics.fmean(p["winner_r"] - p["loser_r"] for p in v), 3),
                "winner_better": sum(1 for p in v if p["winner_r"] > p["loser_r"])}
            for k, v in sorted(by_matchup.items(), key=lambda kv: -len(kv[1]))
        },
        "pairs": pairs,
    }


def render(report: dict) -> str:
    if report.get("error"):
        sk = report.get("skipped") or {}
        detail = ""
        if sk:
            detail = (f" | winner_unresolved={sk.get('winner_unresolved', 0)} "
                      f"loser_ungraded={sk.get('loser_ungraded', 0)} "
                      f"unbuildable={sk.get('unbuildable', 0)}")
        return f"CONTEST-AUDIT | {report['error']}{detail}"

    o = report["overall"]
    lines = [
        f"CONTEST-AUDIT | {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"| contests={report['sample']} paired={len(report['pairs'])}",
    ]
    if o:
        lines.append(
            f"{'paired':<12} meanDiff={o['mean_diff']:+.3f}R sd={o['sd']:.2f} "
            f"n={o['n']} eff={o['eff_n']} se={o['se']:.3f} t={o['t']:+.2f} "
            f"(nom {o['t_nominal']:+.2f}) df={o['df']} p={o['p']:.3f}"
        )
        lines.append(
            f"{'':<12} winner beat the candidate it displaced on "
            f"{o['winner_better']}/{o['n']} contests"
        )
    else:
        lines.append(f"{'paired':<12} too few pairs to compute a difference")
    for key, cell in report["by_matchup"].items():
        lines.append(
            f"{key:<12} n={cell['n']} meanDiff={cell['mean_diff']:+.3f}R "
            f"winner ahead {cell['winner_better']}/{cell['n']}"
        )
    sk = report.get("skipped") or {}
    if sum(sk.values()):
        lines.append(
            f"{'skipped':<12} {sk.get('winner_unresolved', 0)} winners not resolved yet, "
            f"{sk.get('loser_ungraded', 0)} losers not graded yet, "
            f"{sk.get('unbuildable', 0)} could not be rebuilt"
        )
    lines.append(
        f"{'note':<12} paired inside one symbol and bar, so the market move cancels "
        f"— charged mean_open={report['overlap']:.2f} all the same"
    )
    return "\n".join(lines)
