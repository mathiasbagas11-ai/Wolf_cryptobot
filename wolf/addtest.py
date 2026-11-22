"""Paired offline test: does averaging down add expectancy, or only win rate? (``/addtest``)

The owner sent the "math atlas" of another bot (Copilot) whose 70% win rate
at a payoff of about 1 comes from adding to a losing position and closing it
at half the gap ("ADD + recovery setengah gap"). Averaging down is known to
reshape outcomes: more small wins, rarer large losses. Whether it changes the
mean is the question, and it is a paired one. The same entries are traded
twice, so the market move cancels and the comparison is cheap.

Rules, fixed before any read (registry ``average-down-half-gap``):

* **Entries:** exactly the trend-breakout trades of ``/trendtest`` A (same
  bars, same side, same 2×ATR initial risk ``R``, same 90-bar cap).
* **Plain:** A as it is, with a trailing stop.
* **ADD:** the first unit is the plain trade, unchanged. If price moves 0.5R
  against the entry, one more lot is added at that level (or at the open, if a
  bar gaps through it). That lot alone is closed at the half-gap target
  ``(entry + add) / 2``, which is how the atlas's "recovery per lot" works. It
  is otherwise closed at the stop the first unit has in force, or at the time
  cap. The target cannot fill on the same bar as the add, while the stop can
  (the conservative order).
* Results in R of the *first* unit. Each unit pays its own 20bps round trip,
  so an added trade pays twice.

Pre-registered reading: ADD counts as improving expectancy only if the
paired net gap (ADD − plain) is positive with |t| ≥ 2. The standard error is
clustered by entry day, because trades on one day are one market. The
prediction written before the read: win rate up, mean net not up, the worst 1%
worse.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Optional, Sequence

from wolf.bbtest import DEFAULT_SYMBOLS, INTERVAL, fetch_history
from wolf.models import Candle
from wolf.trendtest import (HOLD_BARS, REGIME_BARS, TRAIL_MULT, btc_regime, run_trend,
                            simulate_trail)

ADD_AT = 0.5               # add one unit after an adverse move of 0.5R
T_LINE = 2.0


def simulate_add_lot(candles: Sequence[Candle], k: int, risk: float, long: bool,
                     hold: int = HOLD_BARS) -> Optional[dict]:
    """The added lot alone, as the Copilot atlas runs it (slide 05).

    The first unit is untouched: an add never changes its stop or its trail,
    so it books exactly the plain trade. The second unit is bought once price
    has moved ``ADD_AT``×R against the entry, before the first unit's stop and
    while its trail still sits below that level. It is closed on its own at the
    half-gap target ``(entry + fill) / 2``, at the stop in force (which it
    shares with the first unit), or at the time cap. ``None`` = no add fired.
    """
    if k + hold > len(candles) or risk <= 0:
        return None
    s = 1 if long else -1
    entry = candles[k].open
    stop = entry - s * risk
    add_level = entry - s * ADD_AT * risk
    trail = TRAIL_MULT * risk
    best = entry
    fill = target = None
    for j in range(k, k + hold):
        b = candles[j]
        adverse = b.low if long else b.high
        favour = b.high if long else b.low
        if fill is None:
            if (adverse - stop) * s <= 0:
                return {"added": False}                # stopped before any add
            if (adverse - add_level) * s <= 0:
                fill = min(add_level, b.open) if long else max(add_level, b.open)
                target = (entry + fill) / 2
                if (adverse - stop) * s <= 0:
                    px = min(stop, b.open) if long else max(stop, b.open)
                    return {"added": True, "r": (px - fill) * s / risk, "how": "stop"}
                continue                               # target cannot fill on the add bar
            best = max(best, b.high) if long else min(best, b.low)
            stop = max(stop, best - trail) if long else min(stop, best + trail)
        else:
            if (adverse - stop) * s <= 0:
                px = min(stop, b.open) if long else max(stop, b.open)
                return {"added": True, "r": (px - fill) * s / risk, "how": "stop"}
            if (favour - target) * s >= 0:
                px = max(target, b.open) if long else min(target, b.open)
                return {"added": True, "r": (px - fill) * s / risk, "how": "target"}
            # the first unit's trail keeps moving while the lot is open
            best = max(best, b.high) if long else min(best, b.low)
            stop = max(stop, best - trail) if long else min(stop, best + trail)
    if fill is None:
        return {"added": False}
    last = candles[k + hold - 1].close
    return {"added": True, "r": (last - fill) * s / risk, "how": "timeout"}


def _clustered_t(diffs: Sequence[float], days: Sequence[str]) -> tuple[float, float, int]:
    """Mean, cluster-robust se (clusters = entry days), number of clusters."""
    n = len(diffs)
    mean = statistics.fmean(diffs)
    sums: dict[str, float] = {}
    for d, day in zip(diffs, days):
        sums[day] = sums.get(day, 0.0) + (d - mean)
    g = len(sums)
    if g < 2:
        return mean, float("inf"), g
    var = sum(v * v for v in sums.values()) / (n * n) * g / (g - 1)
    return mean, math.sqrt(var), g


def _pct(xs: Sequence[float], q: float) -> float:
    srt = sorted(xs)
    return srt[max(0, min(len(srt) - 1, int(q * (len(srt) - 1))))]


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch_history(sym, client)
            sources[sym] = source
            if len(candles) > REGIME_BARS + HOLD_BARS + 50:
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources}
    btc = series.get("BTCUSDT") or (series[next(iter(series))] if series else None)
    if not series or btc is None:
        report["error"] = "no candle history could be fetched for any symbol"
        return report
    report["from"] = time.strftime("%Y-%m-%d", time.gmtime(min(c[0].time for c in series.values()) / 1000))
    regime = btc_regime(btc)
    rows = []
    for sym, candles in series.items():
        for t in run_trend(candles, regime):
            k = t["bar"]
            entry = candles[k].open
            risk = entry * t["risk_pct"] / 100
            plain = simulate_trail(candles, k, risk, t["long"])
            lot = simulate_add_lot(candles, k, risk, t["long"])
            if plain is None or lot is None:
                continue
            unit_cost = (cost_bps / 100.0) / t["risk_pct"]
            p_net = plain[0] - unit_cost
            lot_net = lot["r"] - unit_cost if lot["added"] else 0.0
            rows.append({
                "plain": p_net, "add": p_net + lot_net, "lot": lot_net if lot["added"] else None,
                "added": lot["added"], "how": lot.get("how"),
                "day": time.strftime("%Y-%m-%d", time.gmtime(candles[k].time / 1000)),
            })
    if not rows:
        report["error"] = "no trades"
        return report
    p = [r["plain"] for r in rows]
    a = [r["add"] for r in rows]
    diffs = [x - y for x, y in zip(a, p)]
    mean, se, g = _clustered_t(diffs, [r["day"] for r in rows])
    t = mean / se if se and se != float("inf") else 0.0
    added = [r for r in rows if r["added"]]
    report.update({
        "n": len(rows), "days": g, "added_n": len(added),
        "plain": {"wr": round(sum(x > 0 for x in p) / len(p) * 100, 1), "net": round(statistics.fmean(p), 4),
                  "p1": round(_pct(p, 0.01), 2), "worst": round(min(p), 2)},
        "add": {"wr": round(sum(x > 0 for x in a) / len(a) * 100, 1), "net": round(statistics.fmean(a), 4),
                "p1": round(_pct(a, 0.01), 2), "worst": round(min(a), 2)},
        "gap": round(mean, 4), "se": round(se, 4), "t": round(t, 2),
        "added_gap": round(statistics.fmean(r["lot"] for r in added), 4) if added else None,
        "lot_wr": round(sum(r["lot"] > 0 for r in added) / len(added) * 100, 1) if added else None,
        "target_hits": sum(1 for r in added if r["how"] == "target"),
    })
    report["verdict"] = ("ADD improves expectancy (pre-registered bar met)" if mean > 0 and t >= T_LINE
                         else "ADD does not improve expectancy")
    return report


def render(report: dict) -> str:
    head = (f"ADD-TEST | averaging down + half-gap exit vs plain, same /trendtest A entries, "
            f"{INTERVAL}, net of {report['cost_bps']:g}bps per unit")
    if "error" in report:
        return head + "\n" + report["error"]
    p, a = report["plain"], report["add"]
    lines = [
        head,
        f"since    {report['from']} · {report['n']} paired trades over {report['days']} entry days · "
        f"ADD triggered on {report['added_n']} ({report['added_n'] / report['n'] * 100:.0f}%), "
        f"half-gap target hit {report['target_hits']}",
        f"PLAIN    win {p['wr']:.0f}% · net {p['net']:+.3f}R · worst 1% {p['p1']:+.2f}R · worst {p['worst']:+.2f}R",
        f"ADD      win {a['wr']:.0f}% · net {a['net']:+.3f}R · worst 1% {a['p1']:+.2f}R · worst {a['worst']:+.2f}R",
        f"gap      ADD − plain {report['gap']:+.4f}R/trade, se {report['se']:.4f} (clustered by day), "
        f"t {report['t']:+.2f} (needs > 0 and t ≥ {T_LINE:g}) — {report['verdict']}",
    ]
    if report["added_gap"] is not None:
        lines.append(f"LOT      the added lot alone: win {report['lot_wr']:.0f}% · net {report['added_gap']:+.3f}R each "
                     f"(wins +0.25R gross at the half-gap target, loses up to 0.5R at the stop)")
    lines.append("note     R is the first unit's risk. The first unit is the plain trade unchanged; ADD adds "
                 "one lot that pays its own round trip. Offline only — changes no signal.")
    return "\n".join(lines)
