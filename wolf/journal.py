"""The ledger shaped as a trading journal, for the dashboard.

Modelled on what journal tools put on their home page — headline numbers,
an equity curve, a profit calendar with weekly totals, a breakdown by setup —
but every figure here is computed the way the rest of the bot computes it:
net of the round-trip cost on each trade's own stop, compounded at the paper
risk, and over the capped outcome log only, which the payload says.

Two things a journal usually shows are left out on purpose. A "best setup"
highlight is the maximum of several noisy means and is positive under a null,
so the setup table is plain and sorted by name. And there is no per-hour or
per-weekday breakdown: twenty-four or seven more buckets scanned by eye is a
forking-path generator, and nothing in the bot's evidence asks that question.
"""

from __future__ import annotations

import statistics
from collections import OrderedDict
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from wolf.models import Signal
from wolf.montecarlo import _trade_rows


def _summary(gross: list[float], net: list[float]) -> dict:
    wins = [r for r in net if r > 0]
    losses = [r for r in net if r < 0]
    gain, pain = sum(wins), -sum(losses)
    return {
        "n": len(net),
        "win_rate": round(len(wins) / len(net) * 100, 1) if net else 0.0,
        "gross_r": round(statistics.fmean(gross), 3) if gross else 0.0,
        "net_r": round(statistics.fmean(net), 3) if net else 0.0,
        "total_net_r": round(sum(net), 1),
        "avg_win_r": round(statistics.fmean(wins), 2) if wins else 0.0,
        "avg_loss_r": round(statistics.fmean(losses), 2) if losses else 0.0,
        "profit_factor": round(gain / pain, 2) if pain else None,
    }


def journal(outcomes: Iterable[Signal], *, risk_pct: float = 1.0,
            round_trip_bps: float = 20.0, tz: str = "Asia/Jakarta") -> Optional[dict]:
    rows = _trade_rows(outcomes, round_trip_bps, None)
    if not rows:
        return None
    zone = ZoneInfo(tz)
    k = risk_pct / 100.0

    curve, gross_eq, net_eq, peak, max_dd = [], 1.0, 1.0, 1.0, 0.0
    for t in rows:
        gross_eq *= max(0.0, 1 + k * t["scale"] * t["gross"])
        net_eq *= max(0.0, 1 + k * t["scale"] * t["net"])
        peak = max(peak, net_eq)
        max_dd = max(max_dd, (peak - net_eq) / peak if peak else 0.0)
        curve.append({"t": int(t["at"].timestamp()),
                      "gross": round((gross_eq - 1) * 100, 2),
                      "net": round((net_eq - 1) * 100, 2)})

    days: "OrderedDict[str, dict]" = OrderedDict()
    for t in rows:
        d = t["at"].astimezone(zone).date().isoformat()
        cell = days.setdefault(d, {"date": d, "n": 0, "net_r": 0.0})
        cell["n"] += 1
        cell["net_r"] += t["net"]
    for cell in days.values():
        cell["net_r"] = round(cell["net_r"], 2)

    by_strategy = {}
    for name in sorted({t["strategy"] for t in rows}):
        sub = [t for t in rows if t["strategy"] == name]
        by_strategy[name] = _summary([t["gross"] for t in sub], [t["net"] for t in sub])

    overall = _summary([t["gross"] for t in rows], [t["net"] for t in rows])
    # The win rate a ladder of this shape needs just to break even, net.
    aw, al = overall["avg_win_r"], overall["avg_loss_r"]
    overall["breakeven_wr"] = round(-al / (aw - al) * 100, 1) if aw > 0 and al < 0 else None
    overall["max_dd_pct"] = round(max_dd * 100, 1)
    overall["net_return_pct"] = curve[-1]["net"]
    overall["gross_return_pct"] = curve[-1]["gross"]

    return {
        "from": rows[0]["at"].astimezone(zone).date().isoformat(),
        "to": rows[-1]["at"].astimezone(zone).date().isoformat(),
        "risk_pct": risk_pct,
        "cost_bps": round_trip_bps,
        "tz": tz,
        "overall": overall,
        "curve": curve,
        "days": list(days.values()),
        "by_strategy": by_strategy,
    }
