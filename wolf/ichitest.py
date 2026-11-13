"""Offline backtest of the two textbook Ichimoku entries (``/ichitest``).

The owner sent a video (@thequantbuilder) that tested Ichimoku 9/26/52 on
Nasdaq and gold M15: the cloud breakout lost −289.6R and the Tenkan/Kijun cross
−549.9R, a thousand random versions matched on direction and hour all lost,
and the one variant that beat 97.1% of random runs was then beaten by its own
out-of-sample: the version that looked best before 2024 made −6R after it. This
asks the same question on the bot's crypto basket, with rules fixed before any
reading (registry ``ichimoku-textbook-entries``). Nothing the bot emits changes.

* **Lines** (causal): Tenkan = mid of the last 9 bars' range, Kijun = 26,
  Span A = (Tenkan + Kijun)/2 and Span B = mid of 52, both plotted 26 bars
  ahead — so the cloud *at* bar i is what was computed at bar i − 26.
* **TK cross**: Tenkan closes above Kijun after being at or below it on the
  previous bar → long. Mirror for short.
* **Cloud breakout**: close above the cloud top after closing at or under it
  on the previous bar → long. Mirror for short.
* Entry at the next bar's open. **Stop 2 × ATR(14)** from entry, **target
  2R**, out at the close after ``HOLD_BARS`` bars. Stop first on a straddling
  bar; gaps fill at the open. One position per rule per coin at a time. The
  video does not state its exits, so these are a stated choice, not a copy.

Four tests (two rules × two sides), so each must beat ``BEAT_LINE`` = 98.75%
of the baseline (5% split four ways) **and** be net positive both before 2024
and from 2024 on — the video's own out-of-sample cut, fixed here in advance.

The baseline keeps each trade's coin, side, stop %, target and time cap, and
draws its entry from bars at the same UTC hour, as the video's did. Only the
moment differs, which is the one thing the rule claims to know.
"""

from __future__ import annotations

import random
import statistics
import time
from typing import Optional, Sequence

from wolf.bbtest import DEFAULT_SYMBOLS, INTERVAL, _atr_series, fetch_history
from wolf.ewtest import simulate
from wolf.models import Candle

TENKAN, KIJUN, SPAN_B, SHIFT = 9, 26, 52, 26
STOP_ATR = 2.0
TARGET_R = 2.0
HOLD_BARS = 60          # 10 days of 4h bars
RANDOM_DRAWS = 300
BEAT_LINE = 98.75
MIN_TRADES = 100
SPLIT_YEAR = 2024
RULES = ("tk_cross", "cloud_break")


# ── lines ───────────────────────────────────────────────────────────────────

def _mid(candles: Sequence[Candle], i: int, n: int) -> Optional[float]:
    if i + 1 < n:
        return None
    window = candles[i + 1 - n:i + 1]
    return (max(c.high for c in window) + min(c.low for c in window)) / 2


def ichimoku(candles: Sequence[Candle]) -> list[Optional[dict]]:
    """Per bar: tenkan, kijun and the cloud that is *showing* at that bar."""
    raw = []
    for i in range(len(candles)):
        t, k, b = _mid(candles, i, TENKAN), _mid(candles, i, KIJUN), _mid(candles, i, SPAN_B)
        raw.append((t, k, None if t is None or k is None else (t + k) / 2, b))
    out: list[Optional[dict]] = []
    for i, (t, k, _, _) in enumerate(raw):
        src = i - SHIFT
        if t is None or k is None or src < 0 or raw[src][2] is None or raw[src][3] is None:
            out.append(None)
            continue
        a, b = raw[src][2], raw[src][3]
        out.append({"tenkan": t, "kijun": k, "top": max(a, b), "bottom": min(a, b)})
    return out


def signals(candles: Sequence[Candle], rule: str) -> list[tuple[int, bool]]:
    """(signal bar, is_long) for every fresh cross; the trade fills a bar later."""
    lines = ichimoku(candles)
    out = []
    for i in range(1, len(candles)):
        now, prev = lines[i], lines[i - 1]
        if now is None or prev is None:
            continue
        if rule == "tk_cross":
            up = now["tenkan"] > now["kijun"] and prev["tenkan"] <= prev["kijun"]
            down = now["tenkan"] < now["kijun"] and prev["tenkan"] >= prev["kijun"]
        else:
            c, p = candles[i].close, candles[i - 1].close
            up = c > now["top"] and p <= prev["top"]
            down = c < now["bottom"] and p >= prev["bottom"]
        if up:
            out.append((i, True))
        elif down:
            out.append((i, False))
    return out


def run_rule(candles: Sequence[Candle], rule: str) -> list[dict]:
    atrs = _atr_series(candles)
    trades, free_from = [], 0
    for i, long in signals(candles, rule):
        k = i + 1
        atr = atrs[i]
        if k < free_from or k >= len(candles) or atr != atr or atr <= 0:
            continue
        entry = candles[k].open
        risk = STOP_ATR * atr
        stop = entry - risk if long else entry + risk
        target = entry + TARGET_R * risk if long else entry - TARGET_R * risk
        if (stop <= 0) or (target <= 0):
            continue
        res = simulate(candles, k, entry, stop, target, long, hold=HOLD_BARS)
        if res is None:
            continue
        r, exit_i, how = res
        trades.append({"r": r, "how": how, "long": long, "bar": k,
                       "time": candles[k].time, "risk_pct": risk / entry * 100})
        free_from = exit_i + 1
    return trades


# ── baseline ────────────────────────────────────────────────────────────────

def _hour(ms: int) -> int:
    return time.gmtime(ms / 1000).tm_hour


def random_baseline(series: dict, trades: dict, cost_bps: float,
                    draws: int = RANDOM_DRAWS, seed: int = 23) -> dict:
    """Mean net R per draw, per side: same coin, side, stop %, target, cap and
    UTC hour as each trade; only the bar differs."""
    rng = random.Random(seed)
    by_hour = {}
    for sym, candles in series.items():
        hi = len(candles) - HOLD_BARS
        pools: dict[int, list[int]] = {}
        for k in range(1, max(hi, 1)):
            pools.setdefault(_hour(candles[k].time), []).append(k)
        by_hour[sym] = pools
    out = {"long": [], "short": []}
    for _ in range(draws):
        sides = {"long": [], "short": []}
        for sym, ts in trades.items():
            candles = series[sym]
            for t in ts:
                pool = by_hour[sym].get(_hour(t["time"]))
                if not pool:
                    continue
                k = rng.choice(pool)
                entry = candles[k].open
                f = t["risk_pct"] / 100
                stop = entry * (1 - f) if t["long"] else entry * (1 + f)
                tgt = entry * (1 + TARGET_R * f) if t["long"] else entry * (1 - TARGET_R * f)
                res = simulate(candles, k, entry, stop, tgt, t["long"], hold=HOLD_BARS)
                if res is None:
                    continue
                sides["long" if t["long"] else "short"].append(
                    res[0] - (cost_bps / 100.0) / t["risk_pct"])
        for side, rs in sides.items():
            if rs:
                out[side].append(statistics.fmean(rs))
    return out


# ── report ──────────────────────────────────────────────────────────────────

def _net(t: dict, cost_bps: float) -> float:
    return t["r"] - (cost_bps / 100.0) / t["risk_pct"]


def _summary(rows: Sequence[dict], cost_bps: float) -> dict:
    if not rows:
        return {"n": 0}
    net = [_net(t, cost_bps) for t in rows]
    return {
        "n": len(rows),
        "win_rate": round(sum(1 for r in net if r > 0) / len(net) * 100, 1),
        "gross": round(statistics.fmean(t["r"] for t in rows), 3),
        "net": round(statistics.fmean(net), 3),
        "total": round(sum(net), 1),
        "risk_pct": round(statistics.median(t["risk_pct"] for t in rows), 2),
    }


def _split(rows: Sequence[dict], cost_bps: float) -> dict:
    before = [t for t in rows if time.gmtime(t["time"] / 1000).tm_year < SPLIT_YEAR]
    after = [t for t in rows if time.gmtime(t["time"] / 1000).tm_year >= SPLIT_YEAR]
    return {"before": _summary(before, cost_bps), "after": _summary(after, cost_bps)}


def _verdict(s: dict) -> str:
    if s["n"] < MIN_TRADES:
        return f"INCONCLUSIVE (n<{MIN_TRADES})"
    halves_ok = all((s[h].get("n") or 0) > 0 and s[h]["net"] > 0 for h in ("before", "after"))
    if s["beats"] >= BEAT_LINE and s["net"] > 0 and halves_ok:
        return "PASSES the pre-registered bar — still one backtest, not a strategy"
    return "does not beat random entries with the same stop, target and hour"


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None, draws: int = RANDOM_DRAWS) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch_history(sym, client)
            sources[sym] = source
            if len(candles) > HOLD_BARS + SPAN_B + SHIFT + 10:
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources, "rules": {}}
    if not series:
        report["error"] = "no candle history could be fetched for any symbol"
        return report
    report["from"] = time.strftime(
        "%Y-%m-%d", time.gmtime(min(c[0].time for c in series.values()) / 1000))
    for rule in RULES:
        trades = {sym: run_rule(c, rule) for sym, c in series.items()}
        every = [t for ts in trades.values() for t in ts]
        base = random_baseline(series, trades, cost_bps, draws=draws)
        out = {}
        for side, is_long in (("long", True), ("short", False)):
            rows = [t for t in every if t["long"] == is_long]
            s = _summary(rows, cost_bps)
            if s["n"]:
                s.update(_split(rows, cost_bps))
                srt = sorted(base.get(side) or [])
                if srt:
                    s["random_median"] = round(statistics.median(srt), 3)
                    s["random_hi"] = round(srt[int(BEAT_LINE / 100 * (len(srt) - 1))], 3)
                    s["beats"] = round(sum(1 for b in srt if b < s["net"]) / len(srt) * 100, 1)
                    s["verdict"] = _verdict(s)
            out[side] = s
        out["per_symbol"] = {sym: _summary(ts, cost_bps) for sym, ts in trades.items()}
        report["rules"][rule] = out
    return report


_NAMES = {"tk_cross": "TENKAN/KIJUN CROSS", "cloud_break": "CLOUD BREAKOUT"}


def render(report: dict) -> str:
    head = (f"ICHI-TEST | Ichimoku {TENKAN}/{KIJUN}/{SPAN_B}, {INTERVAL}, stop {STOP_ATR:g}×ATR, "
            f"target {TARGET_R:g}R, out after {HOLD_BARS} bars, net of {report['cost_bps']:g}bps")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head, f"since    {report['from']}  (out-of-sample cut: {SPLIT_YEAR})"]
    short_hist = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    missing = [s for s, src in report["sources"].items() if src == "unavailable"]
    if short_hist:
        lines.append(f"WARNING  only ~5.5 months of history for {', '.join(short_hist)}")
    if missing:
        lines.append(f"skipped  no data: {', '.join(missing)}")
    for rule, out in report["rules"].items():
        lines.append(_NAMES.get(rule, rule))
        for side in ("long", "short"):
            s = out.get(side) or {}
            if not s.get("n"):
                lines.append(f"  {side.upper():<5} no trades")
                continue
            lines.append(f"  {side.upper():<5} n={s['n']:<4} wr={s['win_rate']:.0f}% gross "
                         f"{s['gross']:+.3f} net {s['net']:+.3f} total {s['total']:+.1f}R | 1R {s['risk_pct']}%")
            b, a = s["before"], s["after"]
            fmt = lambda h: (f"n={h['n']} net {h['net']:+.3f}" if h.get("n") else "no trades")  # noqa: E731
            lines.append(f"        before {SPLIT_YEAR}: {fmt(b)} | {SPLIT_YEAR}+: {fmt(a)}")
            if "beats" in s:
                lines.append(f"        random same coin/side/stop/target/hour: median "
                             f"{s['random_median']:+.3f}, p{BEAT_LINE:g} {s['random_hi']:+.3f} → "
                             f"beats {s['beats']:.0f}% (needs {BEAT_LINE:g}% and both halves > 0)"
                             f" — {s['verdict']}")
        best = sorted(((sym, d) for sym, d in out["per_symbol"].items() if d.get("n")),
                      key=lambda kv: kv[1]["net"])
        if best:
            lines.append("  per coin net: " + ", ".join(
                f"{sym.replace('USDT', '')} {d['net']:+.2f}" for sym, d in best))
    lines.append("note     rules fixed before reading, not tuned; the best coin is a max over "
                 "ten, not a lead. Offline only — changes no signal.")
    return "\n".join(lines)
