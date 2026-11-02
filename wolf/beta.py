"""How much of each trade's R is just the market moving under it (``/beta``).

Every card so far asks whether the ledger makes money. None asks where the
money it does make, or lose, came from. Most of the universe moves with BTC,
so a long opened before a green afternoon books a gain whatever its detector
read, and a day when every strategy stops out together (09-28) is one market
move seen through six labels. This card separates the two parts.

For every resolved trade it measures how far BTC moved while the position was
open, signed so that a positive number means BTC moved the trade's way, and
expressed in the trade's *own* R (the BTC move in percent divided by the
trade's stop distance in percent). Then:

* ``R = a + b·x`` across all trades. ``b`` near 1 means trades rode BTC
  roughly one for one; ``b·x̄`` is the part of the mean R that BTC's drift
  supplied, and ``a`` is what is left. A positive ``b`` is expected by
  construction (alts follow BTC, and a long gains when its coin rises), so the
  readings that carry information are ``x̄`` — did the bot sit on BTC's side
  or against it — and how much of the mean ``b·x̄`` accounts for.
* The same split per strategy and per direction, with the pooled ``b``, so a
  strategy's run can be read beside the market it ran in. Each group's rest
  is tested for zero and goes through BH-FDR with the others.
* A plain WITH / AGAINST count: trades where BTC moved their way versus not,
  and the mean R of each.

Choices fixed before any read (registry ``btc-beta-explains-the-ledger``):
BTC is the baseline and ETH the one sensitivity check; gross R is regressed
and net is the gross rest less each group's mean cost; ``x`` is clipped to
±``X_CLIP`` R, because R itself lives between -1 and the last rung and a
tiny stop under a big BTC move would otherwise decide the slope alone;
standard errors are clustered by the UTC day the trade closed, because trades
open together share one BTC move and are not separate observations.

The intercept is in-sample and is never alpha. Nothing here changes a signal.
"""

from __future__ import annotations

import bisect
import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from wolf.models import Signal, Status
from wolf.stats import DEFAULT_FDR, benjamini_hochberg, t_to_p
from wolf.tracker import _activation_time, _parse_iso, _risk_pct, r_multiple_of

log = logging.getLogger("wolf.beta")

BASELINE = "BTCUSDT"
SENSITIVITY = "ETHUSDT"
#: 1000 hourly bars reach ~41 days back, past the capped log's ~25.
INTERVAL = "1h"
BAR_MS = 3_600_000
MAX_BARS = 1000
#: R lives in [-1, last rung]; a BTC move worth more R than this on a tiny
#: stop is clipped rather than allowed to set the slope on its own.
X_CLIP = 3.0
#: Fewer distinct days than this and the clustered error has no meaning.
MIN_CLUSTERS = 5


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _closed_at(sig: Signal) -> Optional[datetime]:
    """When the position stopped moving: the exit bar, else the booking."""
    for raw in (sig.exit_time, sig.resolved_at):
        if raw:
            try:
                return _aware(_parse_iso(raw))
            except (TypeError, ValueError):
                continue
    return None


def price_at(bars: list, times: list[int], t: datetime) -> Optional[float]:
    """The baseline's price at ``t``, interpolated inside the hourly bar.

    ``None`` outside the fetched history: a window that cannot be priced is
    skipped and counted, never priced off the nearest bar we happen to hold.
    """
    ms = t.timestamp() * 1000
    i = bisect.bisect_right(times, ms) - 1
    if i < 0 or ms > times[i] + BAR_MS:
        return None
    bar = bars[i]
    frac = min(1.0, max(0.0, (ms - bar.time) / BAR_MS))
    return bar.open + (bar.close - bar.open) * frac


def _rows(outcomes: Iterable[Signal], bars: list, round_trip_bps: float,
          since: Optional[datetime]) -> tuple[list[dict], int]:
    times = [b.time for b in bars]
    rows, unpriced = [], 0
    for o in outcomes:
        if o.status == Status.INVALIDATED.value or not Status(o.status).is_terminal:
            continue
        end = _closed_at(o)
        if end is None or (since is not None and end < since):
            continue
        risk = _risk_pct(o)
        try:
            start = _aware(_activation_time(o, _aware(_parse_iso(o.created_at))))
        except (TypeError, ValueError):
            start = None
        if not risk or start is None:
            continue
        p0, p1 = price_at(bars, times, start), price_at(bars, times, end)
        if not p0 or p1 is None:
            unpriced += 1
            continue
        move = (p1 / p0 - 1) * 100 * (1 if o.is_long else -1)
        x = move / risk
        rows.append({
            "y": r_multiple_of(o),
            "x": max(-X_CLIP, min(X_CLIP, x)),
            "clipped": abs(x) > X_CLIP,
            "cost": (round_trip_bps / 100.0) / risk,
            "day": end.date().isoformat(),
            "strategy": (o.strategy or "?").upper(),
            "side": "LONG" if o.is_long else "SHORT",
        })
    return rows, unpriced


def clustered_ols(ys: list[float], xs: list[float], days: list[str]) -> Optional[dict]:
    """``y = a + b·x`` with standard errors clustered by day (CR1).

    Two trades closing on the same day rode much the same BTC move, so they
    are counted as one cluster; the degrees of freedom are the number of days
    less one, which is the honest count. The undiscounted t travels beside it
    as ``t_nom`` for the size of the discount, as on the card.
    """
    n = len(ys)
    groups = sorted(set(days))
    g = len(groups)
    if n < 3 or g < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - a - b * x for x, y in zip(xs, ys)]
    # (X'X)^-1 for X = [1, x]
    s1, sx, sxx_raw = float(n), sum(xs), sum(x * x for x in xs)
    det = s1 * sxx_raw - sx * sx
    inv = ((sxx_raw / det, -sx / det), (-sx / det, s1 / det))
    meat = [[0.0, 0.0], [0.0, 0.0]]
    for day in groups:
        u0 = sum(e for e, d in zip(resid, days) if d == day)
        u1 = sum(e * x for e, x, d in zip(resid, xs, days) if d == day)
        meat[0][0] += u0 * u0
        meat[0][1] += u0 * u1
        meat[1][0] += u1 * u0
        meat[1][1] += u1 * u1
    c = g / (g - 1) * (n - 1) / (n - 2)

    def sandwich(i: int) -> float:
        return c * sum(inv[i][j] * meat[j][k] * inv[k][i] for j in range(2) for k in range(2))

    se_a, se_b = math.sqrt(max(sandwich(0), 0.0)), math.sqrt(max(sandwich(1), 0.0))
    s2 = sum(e * e for e in resid) / (n - 2)
    se_b_nom = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    sst = sum((y - my) ** 2 for y in ys)
    df = g - 1
    t_b = b / se_b if se_b else 0.0
    return {
        "n": n, "days": g, "df": df,
        "a": a, "b": b, "se_a": se_a, "se_b": se_b,
        "t_a": a / se_a if se_a else 0.0, "t_b": t_b,
        "p_b": t_to_p(t_b, df) if se_b else 1.0,
        "t_b_nom": b / se_b_nom if se_b_nom else 0.0,
        "r2": 1 - sum(e * e for e in resid) / sst if sst > 0 else 0.0,
        "mean_x": mx, "mean_y": my,
    }


def _clustered_mean(vals: list[float], days: list[str]) -> tuple[float, float, int]:
    """Mean of ``vals`` with its standard error clustered by day."""
    n, groups = len(vals), sorted(set(days))
    g = len(groups)
    m = statistics.fmean(vals)
    if g < 2:
        return m, 0.0, 0
    sums = [sum(v - m for v, d in zip(vals, days) if d == day) for day in groups]
    var = g / (g - 1) * sum(s * s for s in sums) / (n * n)
    return m, math.sqrt(var), g - 1


def _group(rows: list[dict], b: float) -> dict:
    ys = [r["y"] for r in rows]
    xs = [r["x"] for r in rows]
    rest_vals = [y - b * x for y, x in zip(ys, xs)]
    rest, se, df = _clustered_mean(rest_vals, [r["day"] for r in rows])
    t = rest / se if se else 0.0
    return {
        "n": len(rows),
        "with_btc": sum(1 for x in xs if x > 0),
        "mean_r": statistics.fmean(ys),
        "mean_x": statistics.fmean(xs),
        "from_btc": b * statistics.fmean(xs),
        "rest": rest,
        "rest_net": rest - statistics.fmean(r["cost"] for r in rows),
        "t": t,
        "p": t_to_p(t, df) if se and df else 1.0,
    }


def _baseline(rows: list[dict]) -> Optional[dict]:
    fit = clustered_ols([r["y"] for r in rows], [r["x"] for r in rows], [r["day"] for r in rows])
    if fit is None:
        return None
    withs = [r["y"] for r in rows if r["x"] > 0]
    against = [r["y"] for r in rows if r["x"] <= 0]
    fit["with_n"], fit["against_n"] = len(withs), len(against)
    fit["with_r"] = statistics.fmean(withs) if withs else None
    fit["against_r"] = statistics.fmean(against) if against else None
    fit["clipped"] = sum(1 for r in rows if r["clipped"])
    fit["mean_cost"] = statistics.fmean(r["cost"] for r in rows)
    return fit


def _fetch(client, symbol: str) -> list:
    try:
        bars = client.get_klines(symbol, interval=INTERVAL, limit=MAX_BARS) or []
    except Exception:
        log.exception("Baseline fetch failed for %s", symbol)
        return []
    return sorted(bars, key=lambda c: c.time)


def beta_report(outcomes: Iterable[Signal], client, *, round_trip_bps: float = 20.0,
                window_hours: float = 0.0, now: Optional[datetime] = None,
                fdr: float = DEFAULT_FDR) -> dict:
    outcomes = list(outcomes)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours) if window_hours > 0 else None
    btc = _fetch(client, BASELINE)
    if not btc:
        return {"error": f"no {INTERVAL} history served for {BASELINE}"}
    rows, unpriced = _rows(outcomes, btc, round_trip_bps, since)
    fit = _baseline(rows) if rows else None
    if fit is None:
        return {"error": "too few priced trades to fit (need 3 across 2+ days)",
                "unpriced": unpriced}

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["side"], []).append(r)
    for r in rows:
        groups.setdefault(r["strategy"], []).append(r)
    table = [{"name": k, **_group(v, fit["b"])} for k, v in groups.items() if len(v) >= 2]
    if table:
        _, padj = benjamini_hochberg([g["p"] for g in table], fdr)
        for g, p in zip(table, padj):
            g["padj"] = p
    order = {"LONG": 0, "SHORT": 1}
    table.sort(key=lambda g: (order.get(g["name"], 2), g["name"]))

    eth = _fetch(client, SENSITIVITY)
    eth_fit = None
    if eth:
        eth_rows, _ = _rows(outcomes, eth, round_trip_bps, since)
        eth_fit = _baseline(eth_rows) if eth_rows else None

    return {
        "error": "", "window_hours": window_hours, "unpriced": unpriced,
        "btc": fit, "eth": eth_fit, "groups": table,
        "thin": fit["days"] < MIN_CLUSTERS,
    }


def _r(x: Optional[float], w: int = 6) -> str:
    return f"{'—':>{w}}" if x is None else f"{x:+{w}.3f}"


def render(report: dict) -> str:
    if report.get("error"):
        extra = f" ({report['unpriced']} trades outside the price history)" if report.get("unpriced") else ""
        return f"BETA: {report['error']}{extra}"
    f = report["btc"]
    window = f"{report['window_hours']:g}h" if report["window_hours"] else "whole log"
    lines = [
        f"BETA vs BTC | {window} | {f['n']} trades over {f['days']} days (gross R)",
        "x = BTC's move while each trade was open, toward the trade (+) or against it (−), "
        "in the trade's own R",
        f"slope b {f['b']:+.2f}R per R of BTC  se {f['se_b']:.2f}  t {f['t_b']:+.2f} "
        f"(nom {f['t_b_nom']:+.2f})  df {f['df']}  R² {f['r2']:.2f}",
        f"meanR {f['mean_y']:+.3f} = from BTC {f['b'] * f['mean_x']:+.3f} "
        f"(x̄ {f['mean_x']:+.3f}) + rest {f['a']:+.3f}  | rest net {f['a'] - f['mean_cost']:+.3f}",
        f"BTC moved WITH the trade: {f['with_n']} trades, meanR {_r(f['with_r'])} | "
        f"AGAINST: {f['against_n']}, meanR {_r(f['against_r'])}",
    ]
    if f["clipped"] or report["unpriced"]:
        lines.append(f"clipped at ±{X_CLIP:g}R: {f['clipped']}   outside BTC history: {report['unpriced']}")
    if report["thin"]:
        lines.append(f"only {f['days']} days — the clustered error needs {MIN_CLUSTERS}+ to mean anything")
    e = report.get("eth")
    if e:
        lines.append(
            f"ETH check: b {e['b']:+.2f} t {e['t_b']:+.2f} R² {e['r2']:.2f} "
            f"from ETH {e['b'] * e['mean_x']:+.3f} rest {e['a']:+.3f}"
        )
    else:
        lines.append("ETH check: no history served")
    if report["groups"]:
        lines.append(f"{'':<9} {'n':>4} {'with%':>5} {'meanR':>7} {'fromBTC':>7} {'rest':>7} "
                     f"{'net':>7}  padj")
        for g in report["groups"]:
            lines.append(
                f"{g['name']:<9} {g['n']:>4} {g['with_btc'] / g['n'] * 100:>5.0f} "
                f"{g['mean_r']:+7.3f} {g['from_btc']:+7.3f} {g['rest']:+7.3f} "
                f"{g['rest_net']:+7.3f}  {g.get('padj', 1.0):.3f}"
            )
    lines.append(
        "read: b>0 is expected (alts follow BTC). The question is x̄ — did the bot sit on "
        "BTC's side — and how much of meanR 'from BTC' carries. 'rest' is in-sample, "
        "never alpha. Changes nothing."
    )
    return "\n".join(lines)
