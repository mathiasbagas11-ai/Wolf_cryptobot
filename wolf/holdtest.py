"""Does v2's breakout add anything over simply holding in BTC's regime? (``/holdtest``)

``/trendtest 1d`` (spot, long only in BTC's UP regime) netted +0.354R a trade
but random days with the same exits earned +0.194R: most of the result was
being long spot crypto while it rose. The owner, who would hold spot, asked
the question that read points at: is the breakout needed at all, or does
plain holding while BTC is above its 300-day mean do as well?

The two are not in the same unit (R per trade against a held basket), so
both are run as **daily spot portfolios** over the same days and compared on
risk-adjusted return:

* **B — breakout.** Exactly ``/trendtest 1d``'s trades (v2's functions, the
  DOWN side of the regime removed). Each trade is bought at its entry open at
  1% of equity at risk (position = 1% / stop %), capped so that the whole
  book never exceeds equity (spot, no leverage), marked open to open, and
  sold at the price the trade closed at.
* **H — regime hold.** When BTC closes above its 300-day mean, the next open
  buys the coins listed that day in equal value and lets them drift; when it
  closes below, the next open sells everything to cash. No rebalancing.

Both pay 10bps a side on everything bought or sold (20bps a round trip, as
everywhere else). No funding (spot). Cash earns nothing.

The test, fixed before any read (registry ``breakout-vs-regime-hold``): the
Sharpe ratio of daily returns, B minus H, over the common period. A
stationary block bootstrap of paired days (30-day blocks, 2000 draws) gives
the share of draws in which B's Sharpe exceeds H's. This is the fifth look
at this rule, so:

* **breakout adds something** only if that share is ≥ 98.75% (1 − 0.05/4)
  and B's Sharpe beats H's in both halves of the period;
* **regime hold beats the breakout** if the share is ≤ 1.25% and H wins
  both halves;
* otherwise **no difference shown** — the breakout is not shown to earn its
  complexity.

CAGR, maximum drawdown, exposure and BTC buy-and-hold are printed beside it
as context, not tested.
"""

from __future__ import annotations

import math
import random
import statistics
import time
from typing import Optional, Sequence

from wolf.bbtest import DEFAULT_SYMBOLS
from wolf.lowtftest import fetch
from wolf.trendtest import REGIME_BARS, btc_regime, run_trend

RISK = 0.01
SIDE_COST = 0.001            # 10bps a side, 20bps a round trip
BLOCK = 30
DRAWS = 2000
PASS_LINE = 98.75
FAIL_LINE = 1.25


def _sharpe(rets: Sequence[float]) -> Optional[float]:
    if len(rets) < 30:
        return None
    sd = statistics.pstdev(rets)
    return statistics.fmean(rets) / sd * math.sqrt(365) if sd > 0 else None


def _stats(rets: Sequence[float], exposure: Sequence[float]) -> dict:
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    years = len(rets) / 365
    cagr = eq ** (1 / years) - 1 if years > 0 and eq > 0 else None
    return {"sharpe": _sharpe(rets), "total_pct": (eq - 1) * 100,
            "cagr_pct": cagr * 100 if cagr is not None else None, "max_dd_pct": dd * 100,
            "exposure_pct": statistics.fmean(exposure) * 100 if exposure else 0.0}


def _timeline(series: dict) -> tuple[list[int], dict[str, dict[int, object]]]:
    btc = series["BTCUSDT"]
    times = [c.time for c in btc]
    return times, {s: {c.time: c for c in cs} for s, cs in series.items()}


def hold_returns(series: dict, up: dict[int, int], times: list[int], by_time: dict,
                 start: int) -> tuple[list[float], list[float]]:
    """Daily returns of H from day ``start``: day t runs open[t] -> open[t+1]."""
    rets, expo = [], []
    held: dict[str, float] = {}               # coin -> value held (equity units)
    cash = 1.0
    for n in range(start, len(times) - 1):
        t, nxt = times[n], times[n + 1]
        want = up.get(times[n - 1]) == 1       # regime read at yesterday's close
        before = cash + sum(held.values())
        if want and not held:
            coins = [s for s in series if t in by_time[s] and nxt in by_time[s]]
            if coins:
                spend = cash / (1 + SIDE_COST)
                held = {s: spend / len(coins) for s in coins}
                cash = 0.0
        elif not want and held:
            cash += sum(held.values()) * (1 - SIDE_COST)
            held = {}
        for s in list(held):
            a, b = by_time[s].get(t), by_time[s].get(nxt)
            if a is None or b is None or a.open <= 0:
                cash += held.pop(s) * (1 - SIDE_COST)     # delisted / gap: sell
                continue
            held[s] *= b.open / a.open
        after = cash + sum(held.values())
        rets.append(after / before - 1)
        expo.append(sum(held.values()) / after if after > 0 else 0.0)
    return rets, expo


def breakout_returns(series: dict, up: dict[int, int], times: list[int], by_time: dict,
                     start: int) -> tuple[list[float], list[float], int]:
    """Daily returns of B over the same days, from ``/trendtest 1d``'s trades."""
    opens: dict[int, list[tuple[str, dict]]] = {}
    count = 0
    for sym, cs in series.items():
        for t in run_trend(cs, up):
            k, j = t["bar"], t.get("exit_bar")
            if j is None:
                continue
            entry = cs[k].open
            exit_px = entry * (1 + t["r"] * t["risk_pct"] / 100)
            opens.setdefault(cs[k].time, []).append(
                (sym, {"exit_time": cs[j].time, "exit_px": exit_px, "w": RISK / (t["risk_pct"] / 100)}))
            count += 1
    rets, expo = [], []
    cash, book = 1.0, {}                       # key -> {sym, value, exit_time, exit_px, last}
    for n in range(start, len(times) - 1):
        t, nxt = times[n], times[n + 1]
        before = cash + sum(p["value"] for p in book.values())
        for sym, spec in opens.get(t, []):
            c = by_time[sym].get(t)
            if c is None or c.open <= 0:
                continue
            want = min(spec["w"], 1.0) * before
            spend = min(want, cash / (1 + SIDE_COST))
            if spend <= 0:
                continue
            cash -= spend * (1 + SIDE_COST)
            book[(sym, t)] = {"sym": sym, "value": spend, "exit_time": spec["exit_time"],
                              "exit_px": spec["exit_px"], "last": c.open}
        for key in list(book):
            p = book[key]
            bar = by_time[p["sym"]].get(t)
            if bar is None:
                continue
            if t == p["exit_time"]:
                p["value"] *= p["exit_px"] / p["last"]
                cash += p["value"] * (1 - SIDE_COST)
                del book[key]
                continue
            nb = by_time[p["sym"]].get(nxt)
            if nb is None:
                continue
            p["value"] *= nb.open / p["last"]
            p["last"] = nb.open
        after = cash + sum(p["value"] for p in book.values())
        rets.append(after / before - 1 if before > 0 else 0.0)
        expo.append(sum(p["value"] for p in book.values()) / after if after > 0 else 0.0)
    return rets, expo, count


def bootstrap(b: Sequence[float], h: Sequence[float], draws: int = DRAWS, block: int = BLOCK,
              seed: int = 53) -> float:
    """% of paired stationary-block resamples where Sharpe(B) > Sharpe(H)."""
    rng = random.Random(seed)
    n = len(b)
    wins = done = 0
    p = 1 / block
    for _ in range(draws):
        idx, i = [], rng.randrange(n)
        while len(idx) < n:
            idx.append(i)
            i = rng.randrange(n) if rng.random() < p else (i + 1) % n
        sb, sh = _sharpe([b[k] for k in idx]), _sharpe([h[k] for k in idx])
        if sb is None or sh is None:
            continue
        done += 1
        wins += sb > sh
    return wins / done * 100 if done else 50.0


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, series: Optional[dict] = None,
             draws: int = DRAWS) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch(sym, "1d", client)
            sources[sym] = source
            if len(candles) > REGIME_BARS + 50:
                series[sym] = candles
    report: dict = {"sources": sources}
    if "BTCUSDT" not in series:
        report["error"] = "no daily BTC history, so there is no regime to read"
        return report
    up = {t: s for t, s in btc_regime(series["BTCUSDT"]).items() if s > 0}
    times, by_time = _timeline(series)
    start = REGIME_BARS + 1
    if len(times) - start < 400:
        report["error"] = "too little daily history after the 300-day regime warm-up"
        return report
    h, h_exp = hold_returns(series, up, times, by_time, start)
    b, b_exp, n_trades = breakout_returns(series, up, times, by_time, start)
    btc = [by_time["BTCUSDT"][times[n + 1]].open / by_time["BTCUSDT"][times[n]].open - 1
           for n in range(start, len(times) - 1)]
    report.update({
        "from": time.strftime("%Y-%m-%d", time.gmtime(times[start] / 1000)),
        "to": time.strftime("%Y-%m-%d", time.gmtime(times[-1] / 1000)),
        "days": len(h), "trades": n_trades,
        "B": _stats(b, b_exp), "H": _stats(h, h_exp), "BTC": _stats(btc, [1.0] * len(btc)),
    })
    mid = len(h) // 2
    report["halves"] = [(_sharpe(b[:mid]), _sharpe(h[:mid])), (_sharpe(b[mid:]), _sharpe(h[mid:]))]
    report["mid"] = time.strftime("%Y-%m-%d", time.gmtime(times[start + mid] / 1000))
    share = bootstrap(b, h, draws=draws)
    report["share"] = round(share, 1)
    b_both = all(x is not None and y is not None and x > y for x, y in report["halves"])
    h_both = all(x is not None and y is not None and y > x for x, y in report["halves"])
    if share >= PASS_LINE and b_both:
        report["verdict"] = "the breakout ADDS something over holding in the regime"
    elif share <= FAIL_LINE and h_both:
        report["verdict"] = "regime HOLD BEATS the breakout"
    else:
        report["verdict"] = "no difference shown — the breakout is not shown to earn its complexity"
    return report


def render(report: dict) -> str:
    head = ("HOLD-TEST | spot, daily: v2's breakout (B, /trendtest 1d trades at 1% risk, no leverage) vs "
            "holding the basket while BTC > its 300-day mean (H); 10bps a side, no funding")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head, f"period   {report['from']} → {report['to']} · {report['days']} days · B made "
                   f"{report['trades']} trades"]
    missing = [s for s, src in report["sources"].items() if src == "unavailable"]
    if missing:
        lines.append(f"skipped  no data: {', '.join(missing)}")
    fmt = lambda v, d=1: "n/a" if v is None else f"{v:+.{d}f}"  # noqa: E731
    for key, name in (("B", "B breakout"), ("H", "H regime hold"), ("BTC", "BTC buy&hold")):
        s = report[key]
        lines.append(f"{name:<14} Sharpe {fmt(s['sharpe'], 2)} · CAGR {fmt(s['cagr_pct'])}% · total "
                     f"{fmt(s['total_pct'], 0)}% · max DD {s['max_dd_pct']:.1f}% · in market "
                     f"{s['exposure_pct']:.0f}%")
    (b1, h1), (b2, h2) = report["halves"]
    lines.append(f"halves   to {report['mid']}: B {fmt(b1, 2)} vs H {fmt(h1, 2)} | after: B {fmt(b2, 2)} "
                 f"vs H {fmt(h2, 2)}")
    lines.append(f"test     Sharpe(B) > Sharpe(H) in {report['share']:.1f}% of 30-day block resamples "
                 f"(adds: ≥{PASS_LINE:g}% + both halves; hold wins: ≤{FAIL_LINE:g}% + both halves) — "
                 f"{report['verdict']}")
    lines.append("note     fifth look at this rule, bar fixed before the read; BTC buy&hold is context, "
                 "not tested. Offline only — changes no signal.")
    return "\n".join(lines)
