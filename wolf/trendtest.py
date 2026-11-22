"""Offline backtest of the two candidates for a new strategy (``/trendtest``).

After two months of the live ledger reading flat before costs and negative
after them, the owner decided to replace the strategy rather than tune it.
The lessons that shaped these two candidates, all from this project's own
cards: costs eat ~0.07–0.09R a trade, so trade less and wider; most of the
ledger was BTC moving under the positions, so either follow the market on
purpose or cancel it; liquid books did better, so stay on majors; and the
only external rule to clear an offline bar (TK cross) is a momentum rule.

Both are fixed before any reading (registry ``trend-and-relative-momentum``):

**A — trend breakout, both directions.**
* Regime from BTC: 4h close above its 300-bar (≈50-day) mean is UP, below is
  DOWN. Longs only in UP, shorts only in DOWN, which is the owner's "do not
  short a top that keeps rising" in rule form.
* Entry: a 4h close beyond the previous 20 bars' extreme in the regime's
  direction; filled at the next open.
* Stop 2×ATR(14) from entry; a trailing stop follows the best price at
  1.5× that distance (3×ATR) and never loosens; no fixed target; out at the
  close after ``HOLD_BARS``. Stop first on a straddling bar; gaps fill at the
  open. One position per coin at a time.

**B — relative momentum, market-neutral.**
* Every Monday 00:00 UTC, rank the basket by its 28-day return; long the top
  ``LEGS``, short the bottom ``LEGS``, equal weight, half the book each side;
  hold one week, out at the next Monday's open. This is the owner's "coins
  that do not move with BTC": it bets on the gap between coins, not on BTC.
* Charged a full round trip on the whole book every week (conservative).

Baselines, each keeping the dependence its rule has (the ``/ichitest``
lesson):
* A — the whole set of trades moved by one random number of whole days, so
  the clustering across coins, each trade's side, stop distance, trailing
  distance and UTC hour survive and only the timing moves.
* B — the same weeks, with a random three long and three short each week, so
  every week's market move is kept and only the ranking is random.

Two tests, so each must beat 97.5% of its baseline, be net positive, and be
net positive both before 2024 and from 2024 on. Funding is not modelled.
"""

from __future__ import annotations

import random
import statistics
import time
from typing import Optional, Sequence

from wolf.bbtest import DEFAULT_SYMBOLS, INTERVAL, _atr_series, fetch_history
from wolf.models import Candle

REGIME_BARS = 300          # ≈ 50 days of 4h bars
BREAKOUT_BARS = 20
STOP_ATR = 2.0
TRAIL_MULT = 1.5           # trailing distance = 1.5 × initial stop = 3×ATR
HOLD_BARS = 90             # 15 days
LOOKBACK_BARS = 168        # 28 days
WEEK_BARS = 42
LEGS = 3
RANDOM_DRAWS = 300
BEAT_LINE = 97.5
MIN_TRADES = 100
MIN_WEEKS = 100
SPLIT_YEAR = 2024
DAY_BARS = 6
_BAR_MS = 4 * 3600 * 1000


# ── A: trend breakout ───────────────────────────────────────────────────────

def btc_regime(btc: Sequence[Candle]) -> dict[int, int]:
    """+1 / −1 per BTC bar time, from the close against its trailing mean."""
    out, total = {}, 0.0
    closes = [c.close for c in btc]
    for i, c in enumerate(closes):
        total += c
        if i >= REGIME_BARS:
            total -= closes[i - REGIME_BARS]
        if i + 1 >= REGIME_BARS:
            out[btc[i].time] = 1 if c > total / REGIME_BARS else -1
    return out


def simulate_trail(candles: Sequence[Candle], k: int, risk: float, long: bool,
                   hold: int = HOLD_BARS) -> Optional[tuple[float, int, str]]:
    """R of one trailing-stop trade entered at bar ``k``'s open.

    The stop in force for a bar is the one set by the bars before it, so a bar
    cannot raise its own stop before testing it. A gap through the stop fills
    at the open.
    """
    if k + hold > len(candles) or risk <= 0:
        return None
    entry = candles[k].open
    trail = TRAIL_MULT * risk
    stop = entry - risk if long else entry + risk
    best = entry
    for j in range(k, k + hold):
        b = candles[j]
        if long:
            if b.low <= stop:
                return (min(stop, b.open) - entry) / risk, j, "stop"
            best = max(best, b.high)
            stop = max(stop, best - trail)
        else:
            if b.high >= stop:
                return (entry - max(stop, b.open)) / risk, j, "stop"
            best = min(best, b.low)
            stop = min(stop, best + trail)
    last = candles[k + hold - 1].close
    return ((last - entry) if long else (entry - last)) / risk, k + hold - 1, "timeout"


def trend_signals(candles: Sequence[Candle], regime: dict[int, int]) -> list[tuple[int, bool]]:
    out = []
    for i in range(BREAKOUT_BARS, len(candles)):
        side = regime.get(candles[i].time)
        if side is None:
            continue
        window = candles[i - BREAKOUT_BARS:i]
        c = candles[i].close
        if side > 0 and c > max(b.high for b in window):
            out.append((i, True))
        elif side < 0 and c < min(b.low for b in window):
            out.append((i, False))
    return out


def run_trend(candles: Sequence[Candle], regime: dict[int, int]) -> list[dict]:
    atrs = _atr_series(candles)
    trades, free_from = [], 0
    for i, long in trend_signals(candles, regime):
        k = i + 1
        if k < free_from or k >= len(candles):
            continue
        atr = atrs[i]
        if atr != atr or atr <= 0:
            continue
        entry = candles[k].open
        risk = STOP_ATR * atr
        if risk >= entry:
            continue
        res = simulate_trail(candles, k, risk, long)
        if res is None:
            continue
        r, exit_i, how = res
        trades.append({"r": r, "how": how, "long": long, "bar": k,
                       "time": candles[k].time, "risk_pct": risk / entry * 100})
        free_from = exit_i + 1
    return trades


def trend_shift_baseline(series: dict, trades: dict, cost_bps: float,
                         draws: int = RANDOM_DRAWS, seed: int = 31) -> list[float]:
    """Mean net R per draw with every trade moved by one random number of days."""
    rng = random.Random(seed)
    span = min(len(c) for c in series.values()) - HOLD_BARS - 1
    out: list[float] = []
    if span <= DAY_BARS * 2:
        return out
    for _ in range(draws):
        days = rng.randrange(1, span // DAY_BARS)
        rs = []
        for sym, ts in trades.items():
            candles = series[sym]
            usable = len(candles) - HOLD_BARS - 1
            for t in ts:
                k = 1 + (t["bar"] - 1 + days * DAY_BARS) % usable
                entry = candles[k].open
                res = simulate_trail(candles, k, entry * t["risk_pct"] / 100, t["long"])
                if res is not None:
                    rs.append(res[0] - (cost_bps / 100.0) / t["risk_pct"])
        if rs:
            out.append(statistics.fmean(rs))
    return out


# ── B: relative momentum ────────────────────────────────────────────────────

def _aligned(series: dict) -> tuple[list[int], dict[str, dict[int, Candle]]]:
    by_time = {sym: {c.time: c for c in cs} for sym, cs in series.items()}
    common = sorted(set.intersection(*(set(m) for m in by_time.values())))
    return common, by_time


def momentum_weeks(series: dict, cost_bps: float) -> list[dict]:
    """One row per Monday: the long/short book's net return for the week."""
    times, by_time = _aligned(series)
    if len(series) < 2 * LEGS:
        return []
    index = {t: n for n, t in enumerate(times)}
    weeks = []
    for n, t in enumerate(times):
        g = time.gmtime(t / 1000)
        if g.tm_wday != 0 or g.tm_hour != 0 or n < LOOKBACK_BARS + 1:
            continue
        end = t + WEEK_BARS * _BAR_MS
        if end not in index:
            continue
        prev, back = times[n - 1], times[n - 1 - LOOKBACK_BARS]
        if prev != t - _BAR_MS or back != prev - LOOKBACK_BARS * _BAR_MS:
            continue                       # a gap in the shared history
        mom = {s: by_time[s][prev].close / by_time[s][back].close - 1 for s in series}
        fwd = {s: by_time[s][end].open / by_time[s][t].open - 1 for s in series}
        ranked = sorted(series, key=lambda s: mom[s])
        weeks.append({"time": t, "fwd": fwd, "ranked": ranked})
    for w in weeks:
        w["net"] = _book(w, w["ranked"][-LEGS:], w["ranked"][:LEGS], cost_bps)
    return weeks


def _book(week: dict, longs: Sequence[str], shorts: Sequence[str], cost_bps: float) -> float:
    lng = statistics.fmean(week["fwd"][s] for s in longs)
    sht = statistics.fmean(-week["fwd"][s] for s in shorts)
    return 0.5 * lng + 0.5 * sht - cost_bps / 10_000


def momentum_baseline(weeks: list[dict], cost_bps: float, draws: int = RANDOM_DRAWS,
                      seed: int = 37) -> list[float]:
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        rs = []
        for w in weeks:
            pick = rng.sample(w["ranked"], 2 * LEGS)
            rs.append(_book(w, pick[:LEGS], pick[LEGS:], cost_bps))
        if rs:
            out.append(statistics.fmean(rs))
    return out


# ── report ──────────────────────────────────────────────────────────────────

def _year(ms: int) -> int:
    return time.gmtime(ms / 1000).tm_year


def _trend_summary(rows: Sequence[dict], cost_bps: float) -> dict:
    if not rows:
        return {"n": 0}
    net = [t["r"] - (cost_bps / 100.0) / t["risk_pct"] for t in rows]
    return {"n": len(rows), "win_rate": round(sum(r > 0 for r in net) / len(net) * 100, 1),
            "gross": round(statistics.fmean(t["r"] for t in rows), 3),
            "net": round(statistics.fmean(net), 3), "total": round(sum(net), 1),
            "best": round(max(net), 2),
            "risk_pct": round(statistics.median(t["risk_pct"] for t in rows), 2)}


def _beats(value: float, draws: Sequence[float]) -> dict:
    srt = sorted(draws)
    return {"median": round(statistics.median(srt), 4),
            "hi": round(srt[int(BEAT_LINE / 100 * (len(srt) - 1))], 4),
            "beats": round(sum(1 for b in srt if b < value) / len(srt) * 100, 1)}


def _verdict(n: int, n_min: int, net: float, beats: float, halves: Sequence[Optional[float]]) -> str:
    if n < n_min:
        return f"INCONCLUSIVE (n<{n_min})"
    if beats >= BEAT_LINE and net > 0 and all(h is not None and h > 0 for h in halves):
        return "PASSES the pre-registered bar — still one backtest, not a strategy"
    return "does not beat its random baseline"


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None, draws: int = RANDOM_DRAWS) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch_history(sym, client)
            sources[sym] = source
            if len(candles) > REGIME_BARS + HOLD_BARS + 50:
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources}
    btc = series.get("BTCUSDT") or (series.get(next(iter(series))) if series else None)
    if not series or btc is None:
        report["error"] = "no candle history could be fetched for any symbol"
        return report
    report["from"] = time.strftime("%Y-%m-%d", time.gmtime(min(c[0].time for c in series.values()) / 1000))

    regime = btc_regime(btc)
    trades = {sym: run_trend(c, regime) for sym, c in series.items()}
    every = [t for ts in trades.values() for t in ts]
    a = _trend_summary(every, cost_bps)
    if a["n"]:
        a["long"] = _trend_summary([t for t in every if t["long"]], cost_bps)
        a["short"] = _trend_summary([t for t in every if not t["long"]], cost_bps)
        a["before"] = _trend_summary([t for t in every if _year(t["time"]) < SPLIT_YEAR], cost_bps)
        a["after"] = _trend_summary([t for t in every if _year(t["time"]) >= SPLIT_YEAR], cost_bps)
        a["per_symbol"] = {s: _trend_summary(ts, cost_bps) for s, ts in trades.items()}
        base = trend_shift_baseline(series, trades, cost_bps, draws=draws)
        if base:
            a.update(_beats(a["net"], base))
            a["verdict"] = _verdict(a["n"], MIN_TRADES, a["net"], a["beats"],
                                    [a["before"].get("net"), a["after"].get("net")])
    report["trend"] = a

    weeks = momentum_weeks(series, cost_bps)
    b: dict = {"weeks": len(weeks)}
    if weeks:
        nets = [w["net"] for w in weeks]
        b["mean_pct"] = round(statistics.fmean(nets) * 100, 3)
        b["win_rate"] = round(sum(r > 0 for r in nets) / len(nets) * 100, 1)
        b["worst_pct"] = round(min(nets) * 100, 2)
        eq, peak, dd = 1.0, 1.0, 0.0
        for r in nets:
            eq *= 1 + r
            peak = max(peak, eq)
            dd = max(dd, 1 - eq / peak)
        b["total_pct"] = round((eq - 1) * 100, 1)
        b["max_dd_pct"] = round(dd * 100, 1)
        before = [w["net"] for w in weeks if _year(w["time"]) < SPLIT_YEAR]
        after = [w["net"] for w in weeks if _year(w["time"]) >= SPLIT_YEAR]
        b["before_pct"] = round(statistics.fmean(before) * 100, 3) if before else None
        b["after_pct"] = round(statistics.fmean(after) * 100, 3) if after else None
        base = momentum_baseline(weeks, cost_bps, draws=draws)
        if base:
            hit = _beats(statistics.fmean(nets), base)
            b.update({"random_median_pct": round(hit["median"] * 100, 3),
                      "random_hi_pct": round(hit["hi"] * 100, 3), "beats": hit["beats"]})
            b["verdict"] = _verdict(len(weeks), MIN_WEEKS, b["mean_pct"], b["beats"],
                                    [b["before_pct"], b["after_pct"]])
    report["momentum"] = b
    return report


def render(report: dict) -> str:
    head = (f"TREND-TEST | two candidates for a new strategy, {INTERVAL}, net of "
            f"{report['cost_bps']:g}bps, funding not modelled")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head, f"since    {report['from']}  (out-of-sample cut: {SPLIT_YEAR})"]
    short_hist = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    missing = [s for s, src in report["sources"].items() if src == "unavailable"]
    if short_hist:
        lines.append(f"WARNING  only ~5.5 months of history for {', '.join(short_hist)}")
    if missing:
        lines.append(f"skipped  no data: {', '.join(missing)}")

    a = report.get("trend") or {}
    lines.append(f"A TREND  BTC regime (300-bar mean), {BREAKOUT_BARS}-bar breakout with the regime, "
                 f"stop {STOP_ATR:g}xATR, trail {STOP_ATR * TRAIL_MULT:g}xATR, out after {HOLD_BARS} bars")
    if not a.get("n"):
        lines.append("  no trades")
    else:
        lines.append(f"  ALL   n={a['n']:<4} wr={a['win_rate']:.0f}% gross {a['gross']:+.3f} net "
                     f"{a['net']:+.3f} total {a['total']:+.1f}R | 1R {a['risk_pct']}% | best {a['best']:+.1f}R")
        for side in ("long", "short"):
            s = a[side]
            lines.append(f"  {side.upper():<5} " + (f"n={s['n']:<4} wr={s['win_rate']:.0f}% net "
                                                     f"{s['net']:+.3f} total {s['total']:+.1f}R"
                                                     if s.get("n") else "no trades"))
        fmt = lambda h: f"n={h['n']} net {h['net']:+.3f}" if h.get("n") else "no trades"  # noqa: E731
        lines.append(f"  before {SPLIT_YEAR}: {fmt(a['before'])} | {SPLIT_YEAR}+: {fmt(a['after'])}")
        if "beats" in a:
            lines.append(f"  same trades shifted by a random number of days: median {a['median']:+.3f}, "
                         f"p{BEAT_LINE:g} {a['hi']:+.3f} → beats {a['beats']:.0f}% "
                         f"(needs {BEAT_LINE:g}% and both halves > 0) — {a['verdict']}")
        per = sorted(((s, d) for s, d in a["per_symbol"].items() if d.get("n")), key=lambda kv: kv[1]["net"])
        lines.append("  per coin net: " + ", ".join(f"{s.replace('USDT', '')} {d['net']:+.2f}" for s, d in per))

    b = report.get("momentum") or {}
    lines.append(f"B MOMENTUM  weekly, rank by {LOOKBACK_BARS // 6}-day return, long top {LEGS} / "
                 f"short bottom {LEGS}, half the book each side")
    if not b.get("weeks"):
        lines.append("  no complete weeks")
    else:
        lines.append(f"  weeks={b['weeks']} mean {b['mean_pct']:+.3f}%/wk win {b['win_rate']:.0f}% "
                     f"total {b['total_pct']:+.1f}% max DD {b['max_dd_pct']:.1f}% worst week {b['worst_pct']:+.2f}%")
        bf = "n/a" if b["before_pct"] is None else f"{b['before_pct']:+.3f}%"
        af = "n/a" if b["after_pct"] is None else f"{b['after_pct']:+.3f}%"
        lines.append(f"  before {SPLIT_YEAR}: {bf}/wk | {SPLIT_YEAR}+: {af}/wk")
        if "beats" in b:
            lines.append(f"  same weeks, random 3 long / 3 short: median {b['random_median_pct']:+.3f}%, "
                         f"p{BEAT_LINE:g} {b['random_hi_pct']:+.3f}% → beats {b['beats']:.0f}% "
                         f"(needs {BEAT_LINE:g}% and both halves > 0) — {b['verdict']}")
    lines.append("note     rules fixed before reading, not tuned; the best coin is a max over ten, "
                 "not a lead. Offline only — changes no signal.")
    return "\n".join(lines)
