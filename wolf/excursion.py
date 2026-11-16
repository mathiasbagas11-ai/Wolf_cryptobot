"""How far trades went for and against them before they resolved (MFE/MAE).

The ledger says where each trade ended; it has never said what path got it
there. A stop-out that first ran +0.9R and a stop-out that never went green
book the same -1R, and an argument about stops or targets is an argument
about those paths, so it cannot be settled from end points.

Every number here is descriptive. Read it as a picture of the paths, never
as a rule to change: "winners rarely dip past 0.6R" invites a tighter stop,
but a tighter stop changes which trades exist and how the ladder is placed,
and exit geometry has already been measured across six variants and does not
move (registry). This card is what any such argument has to be checked
against, not a replacement for checking it.

Rows written before the fields existed carry ``None`` and are left out by
name, not counted as zero excursion.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from wolf.models import Signal, Status
from wolf.tracker import _parse_iso

#: Losers that first reached this far into profit are counted, because a
#: stop-out after +0.5R is a different trade from one that never went green.
LOSER_REACH_R = 0.5
#: Winners whose worst point passed this are counted: they survived a dip
#: most of the way to the stop.
WINNER_DIP_R = 0.8


def _when(o: Signal) -> Optional[datetime]:
    for raw in (o.resolved_at, o.exit_time):
        if raw:
            try:
                t = _parse_iso(raw)
                return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _median(xs: list[float]) -> Optional[float]:
    return round(statistics.median(xs), 2) if xs else None


def _block(rows: list[Signal]) -> dict:
    winners = [o for o in rows if (o.r_multiple or 0) > 0]
    losers = [o for o in rows if (o.r_multiple or 0) < 0]
    return {
        "n": len(rows),
        "mfe_median": _median([o.mfe_r for o in rows]),
        "mae_median": _median([o.mae_r for o in rows]),
        "winners": len(winners),
        "winner_mae_median": _median([o.mae_r for o in winners]),
        "winners_deep_dip": sum(1 for o in winners if o.mae_r >= WINNER_DIP_R),
        "losers": len(losers),
        "loser_mfe_median": _median([o.mfe_r for o in losers]),
        "losers_were_green": sum(1 for o in losers if o.mfe_r >= LOSER_REACH_R),
    }


def excursions(outcomes: Iterable[Signal], *, window_hours: float = 0.0,
               now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours) if window_hours > 0 else None
    traded, unrecorded = [], 0
    for o in outcomes:
        if o.status == Status.INVALIDATED.value or not Status(o.status).is_terminal:
            continue
        at = _when(o)
        if since is not None and (at is None or at < since):
            continue
        if o.mfe_r is None or o.mae_r is None:
            unrecorded += 1
            continue
        traded.append(o)
    by: dict[str, list[Signal]] = {}
    for o in traded:
        by.setdefault((o.strategy or "?").upper(), []).append(o)
    return {
        "window_hours": window_hours,
        "unrecorded": unrecorded,
        "overall": _block(traded),
        "by_strategy": {k: _block(v) for k, v in sorted(by.items())},
    }


def _fmt(x: Optional[float]) -> str:
    return "  —  " if x is None else f"{x:5.2f}"


def render(report: dict) -> str:
    window = f"{report['window_hours']:g}h" if report["window_hours"] else "whole log"
    o = report["overall"]
    lines = [f"EXCURSION (MFE/MAE) | {window} | {o['n']} resolved trades with a recorded path"]
    if report["unrecorded"]:
        lines.append(f"left out: {report['unrecorded']} rows resolved before paths were recorded (2026-10-28)")
    if not o["n"]:
        lines.append("nothing to read yet — paths are recorded from 2026-10-28 on")
        return "\n".join(lines)
    lines += [
        "R = the trade's own stop distance; MFE best point, MAE worst point while open",
        f"{'':<9} {'n':>4} {'MFE':>5} {'MAE':>5} | win {'MAE':>5} dip≥{WINNER_DIP_R:g} | "
        f"loss {'MFE':>5} green≥{LOSER_REACH_R:g}",
    ]

    def row(name: str, b: dict) -> str:
        return (f"{name:<9} {b['n']:>4} {_fmt(b['mfe_median'])} {_fmt(b['mae_median'])} | "
                f"{b['winners']:>3} {_fmt(b['winner_mae_median'])} {b['winners_deep_dip']:>6} | "
                f"{b['losers']:>4} {_fmt(b['loser_mfe_median'])} {b['losers_were_green']:>8}")

    lines.append(row("ALL", o))
    for name, b in report["by_strategy"].items():
        lines.append(row(name, b))
    lines.append(
        "medians; describes the paths, changes nothing. Exit geometry is settled "
        "(not a lever) — this is what any stop/target argument must be checked against"
    )
    return "\n".join(lines)
