"""Offline backtest of an Elliott Wave "wave 2 → wave 3" entry (``/ewtest``).

The owner reads Elliott Wave by hand and asked whether it could join the bot.
A wave count is subjective — the same chart carries several valid counts, and
a count drawn after the fact is always right — so before it goes anywhere near
the signal path it is turned into fixed rules a computer can apply bar by bar,
without seeing the future, and measured offline. Nothing the bot emits changes.

The rules, fixed before any reading (registry ``elliott-wave2-entry``):

* **Swings** come from a causal zigzag: a high or low is a pivot only once
  price has moved ``ZZ_ATR`` × ATR(14) away from it, and it is usable from the
  bar that confirmed it, never earlier.
* **Wave 1** is the last confirmed swing (low L0 → high H1 for a long). The
  setup is live from the bar that confirms H1.
* **Wave 2** must retrace into the 50–61.8% zone of wave 1. A low deeper than
  61.8% — and so any low under L0 — kills the setup; so does a new high above
  H1 before entry, or ``MAX_WAIT`` bars without a trigger.
* **Entry**: once price has reached the zone, the first bar that closes above
  the previous bar's high; filled at the next bar's open.
* **Stop** at the wave-2 low; **target** wave 3 = L2 + 1.618 × wave 1; out at
  the close after ``HOLD_BARS`` bars otherwise. A bar touching both is booked
  as the stop. Gaps through either fill at the open.
* Shorts are the mirror image and are reported separately.

The baseline is what the rule has to beat, not zero. For every trade the rule
took, random entries on the same coin, in the same direction, with the *same*
stop distance, target multiple and time cap — so the only thing that differs
is *when* the trade was entered, which is the one thing a wave count claims to
know. Whatever both earn is the market's drift.
"""

from __future__ import annotations

import random
import statistics
import time
from typing import Optional, Sequence

from wolf.bbtest import DEFAULT_SYMBOLS, INTERVAL, _atr_series, fetch_history
from wolf.models import Candle

ZZ_ATR = 3.0
ZONE = (0.5, 0.618)
EXT = 1.618
MAX_WAIT = 30
HOLD_BARS = 60          # 10 days of 4h bars
RANDOM_DRAWS = 300
#: Each side is its own test, so each must clear 97.5% (a two-test split of 5%).
BEAT_LINE = 97.5
MIN_TRADES = 100


# ── swings ──────────────────────────────────────────────────────────────────

def zigzag(candles: Sequence[Candle], k: float = ZZ_ATR) -> list[dict]:
    """Confirmed swing pivots, each with the bar index that confirmed it.

    A pivot's price and bar are known only in hindsight; ``confirmed`` is the
    first bar at which the reversal from it reached ``k`` × ATR, and nothing
    downstream may use the pivot before that bar closes.
    """
    atrs = _atr_series(candles)
    pivots: list[dict] = []
    direction = 0          # +1 hunting a high, -1 hunting a low
    ext_px, ext_i = None, None
    for i, c in enumerate(candles):
        atr = atrs[i]
        if atr != atr:     # NaN until the ATR has a full window
            continue
        thr = k * atr
        if direction == 0:
            if ext_px is None:
                ext_px, ext_i, lo_px, lo_i = c.high, i, c.low, i
                continue
            if c.high > ext_px:
                ext_px, ext_i = c.high, i
            if c.low < lo_px:
                lo_px, lo_i = c.low, i
            # Whichever side first moves a full threshold away from its own
            # extreme decides the first pivot.
            if c.high - lo_px >= thr:
                pivots.append({"kind": "L", "px": lo_px, "i": lo_i, "confirmed": i})
                direction, ext_px, ext_i = 1, c.high, i
            elif ext_px - c.low >= thr:
                pivots.append({"kind": "H", "px": ext_px, "i": ext_i, "confirmed": i})
                direction, ext_px, ext_i = -1, c.low, i
            continue
        if direction == 1:
            if c.high > ext_px:
                ext_px, ext_i = c.high, i
            elif ext_px - c.low >= thr:
                pivots.append({"kind": "H", "px": ext_px, "i": ext_i, "confirmed": i})
                direction, ext_px, ext_i = -1, c.low, i
        else:
            if c.low < ext_px:
                ext_px, ext_i = c.low, i
            elif c.high - ext_px >= thr:
                pivots.append({"kind": "L", "px": ext_px, "i": ext_i, "confirmed": i})
                direction, ext_px, ext_i = 1, c.high, i
    return pivots


# ── setup ───────────────────────────────────────────────────────────────────

def find_setup(candles: Sequence[Candle], p0: dict, p1: dict) -> Optional[dict]:
    """Walk forward from the bar that confirmed wave 1's end, looking for entry.

    ``p0`` → ``p1`` is wave 1 (L→H for a long, H→L for a short). Returns the
    entry bar, entry, stop, target and direction, or ``None`` when the setup
    dies first.
    """
    long = p0["kind"] == "L"
    wave = abs(p1["px"] - p0["px"])
    if wave <= 0:
        return None
    sign = 1 if long else -1
    zone_near = p1["px"] - sign * ZONE[0] * wave
    zone_far = p1["px"] - sign * ZONE[1] * wave
    extreme = None          # the wave-2 low (long) or high (short) so far
    armed = False
    start = p1["i"] + 1
    for j in range(start, min(len(candles) - 1, p1["confirmed"] + MAX_WAIT + 1)):
        c = candles[j]
        far = c.low if long else c.high
        if extreme is None or (far < extreme if long else far > extreme):
            extreme = far
        beyond_far = extreme < zone_far if long else extreme > zone_far
        if beyond_far:
            return None                                    # deeper than 61.8%
        if (c.high > p1["px"]) if long else (c.low < p1["px"]):
            return None                                    # wave 1's end retaken
        if (extreme <= zone_near) if long else (extreme >= zone_near):
            armed = True
        if j < p1["confirmed"] or not armed:
            continue                                       # cannot act before wave 1 is known
        prev = candles[j - 1]
        broke = c.close > prev.high if long else c.close < prev.low
        if broke:
            entry = candles[j + 1].open
            stop = extreme
            target = extreme + sign * EXT * wave
            if (entry - stop) * sign <= 0 or (target - entry) * sign <= 0:
                return None                                # no room left either side
            return {"bar": j + 1, "entry": entry, "stop": stop, "target": target,
                    "long": long, "signal_bar": j}
    return None


def simulate(candles: Sequence[Candle], k: int, entry: float, stop: float,
             target: float, long: bool, hold: int = HOLD_BARS) -> Optional[tuple[float, int, str]]:
    """R of one trade entered at bar ``k``'s open, and how it ended.

    Stop first when a bar reaches both. Gaps fill at the open they gapped to.
    ``None`` when there are not enough bars left to finish the trade.
    """
    if k + hold > len(candles):
        return None
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        return None
    for j in range(k, k + hold):
        b = candles[j]
        if long:
            if b.low <= stop:
                return (min(stop, b.open) - entry) / risk, j, "stop"
            if b.high >= target:
                return (max(target, b.open) - entry) / risk, j, "target"
        else:
            if b.high >= stop:
                return (entry - max(stop, b.open)) / risk, j, "stop"
            if b.low <= target:
                return (entry - min(target, b.open)) / risk, j, "target"
    last = candles[k + hold - 1].close
    return ((last - entry) if long else (entry - last)) / risk, k + hold - 1, "timeout"


def run_rule(candles: Sequence[Candle]) -> list[dict]:
    """Every trade the rule takes on one coin, one position at a time."""
    pivots = zigzag(candles)
    trades, free_from = [], 0
    for a, b in zip(pivots, pivots[1:]):
        setup = find_setup(candles, a, b)
        if setup is None or setup["bar"] < free_from:
            continue
        res = simulate(candles, setup["bar"], setup["entry"], setup["stop"],
                       setup["target"], setup["long"])
        if res is None:
            continue
        r, exit_i, how = res
        risk = abs(setup["entry"] - setup["stop"])
        trades.append({
            "r": r, "how": how, "long": setup["long"], "bar": setup["bar"],
            "time": candles[setup["bar"]].time,
            "risk_pct": risk / setup["entry"] * 100,
            "target_r": abs(setup["target"] - setup["entry"]) / risk,
        })
        free_from = exit_i + 1
    return trades


# ── baseline ────────────────────────────────────────────────────────────────

def random_baseline(series: dict[str, Sequence[Candle]], trades: dict[str, list[dict]],
                    cost_bps: float, draws: int = RANDOM_DRAWS, seed: int = 17) -> dict:
    """Net mean R of random entries with each trade's own geometry, per side.

    For every trade, a random bar on the same coin is entered in the same
    direction with the same stop distance (% of entry), target multiple and
    time cap. Only the entry moment differs.
    """
    rng = random.Random(seed)
    out = {"long": [], "short": []}
    for _ in range(draws):
        sides = {"long": [], "short": []}
        for sym, ts in trades.items():
            candles = series[sym]
            hi = len(candles) - HOLD_BARS
            if hi <= 1:
                continue
            for t in ts:
                k = rng.randrange(1, hi)
                entry = candles[k].open
                f = t["risk_pct"] / 100
                stop = entry * (1 - f) if t["long"] else entry * (1 + f)
                tgt = (entry + t["target_r"] * (entry - stop)) if t["long"] \
                    else (entry - t["target_r"] * (stop - entry))
                res = simulate(candles, k, entry, stop, tgt, t["long"])
                if res is None:
                    continue
                sides["long" if t["long"] else "short"].append(
                    res[0] - (cost_bps / 100.0) / t["risk_pct"])
        for side, rs in sides.items():
            if rs:
                out[side].append(statistics.fmean(rs))
    return out


# ── report ──────────────────────────────────────────────────────────────────

def _summary(rows: Sequence[dict], cost_bps: float) -> dict:
    if not rows:
        return {"n": 0}
    net = [t["r"] - (cost_bps / 100.0) / t["risk_pct"] for t in rows]
    return {
        "n": len(rows),
        "win_rate": round(sum(1 for r in net if r > 0) / len(net) * 100, 1),
        "gross": round(statistics.fmean(t["r"] for t in rows), 3),
        "net": round(statistics.fmean(net), 3),
        "total": round(sum(net), 1),
        "target": sum(1 for t in rows if t["how"] == "target"),
        "stop": sum(1 for t in rows if t["how"] == "stop"),
        "timeout": sum(1 for t in rows if t["how"] == "timeout"),
        "risk_pct": round(statistics.median(t["risk_pct"] for t in rows), 2),
    }


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None, draws: int = RANDOM_DRAWS) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch_history(sym, client)
            sources[sym] = source
            if len(candles) > HOLD_BARS + 50:
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources, "per_symbol": {}}
    if not series:
        report["error"] = "no candle history could be fetched for any symbol"
        return report

    trades = {sym: run_rule(c) for sym, c in series.items()}
    every = [t for ts in trades.values() for t in ts]
    longs = [t for t in every if t["long"]]
    shorts = [t for t in every if not t["long"]]
    report["long"] = _summary(longs, cost_bps)
    report["short"] = _summary(shorts, cost_bps)
    for sym, ts in trades.items():
        report["per_symbol"][sym] = {
            "from": time.strftime("%Y-%m-%d", time.gmtime(series[sym][0].time / 1000)),
            "all": _summary(ts, cost_bps),
        }
    years: dict[int, list[dict]] = {}
    for t in every:
        years.setdefault(time.gmtime(t["time"] / 1000).tm_year, []).append(t)
    report["years"] = {y: _summary(rows, cost_bps) for y, rows in sorted(years.items())}

    base = random_baseline(series, trades, cost_bps, draws=draws)
    for side, rows in (("long", longs), ("short", shorts)):
        draws_ = base.get(side) or []
        s = report[side]
        if not draws_ or not s.get("n"):
            continue
        srt = sorted(draws_)
        s["random_median"] = round(statistics.median(srt), 3)
        s["random_p975"] = round(srt[int(0.975 * (len(srt) - 1))], 3)
        s["beats"] = round(sum(1 for b in srt if b < s["net"]) / len(srt) * 100, 1)
        s["verdict"] = _verdict(s, report["years"], side)
    return report


def _verdict(s: dict, years: dict, side: str) -> str:
    if s["n"] < MIN_TRADES:
        return f"INCONCLUSIVE (n<{MIN_TRADES})"
    if s["beats"] >= BEAT_LINE and s["net"] > 0:
        return "PASSES the pre-registered bar — still one backtest, not a strategy"
    return "does not beat random entries with the same stop and target"


def render(report: dict) -> str:
    head = (f"EW-TEST | wave 2 → 3 entry, {INTERVAL}, zigzag {ZZ_ATR:g}×ATR, zone "
            f"{ZONE[0]:.0%}–{ZONE[1]:.1%}, target {EXT}×W1, out after {HOLD_BARS} bars, "
            f"net of {report['cost_bps']:g}bps")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head]
    short_hist = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    missing = [s for s, src in report["sources"].items() if src == "unavailable"]
    if short_hist:
        lines.append(f"WARNING  only ~5.5 months of history for {', '.join(short_hist)}")
    if missing:
        lines.append(f"skipped  no data: {', '.join(missing)}")
    for side in ("long", "short"):
        s = report.get(side) or {}
        if not s.get("n"):
            lines.append(f"{side.upper():<6} no trades")
            continue
        lines.append(
            f"{side.upper():<6} n={s['n']:<4} wr={s['win_rate']:.0f}% gross {s['gross']:+.3f} "
            f"net {s['net']:+.3f} total {s['total']:+.1f}R | target {s['target']} stop {s['stop']} "
            f"timeout {s['timeout']} | 1R {s['risk_pct']}%")
        if "beats" in s:
            lines.append(
                f"       random same coin/side/stop/target: median {s['random_median']:+.3f}, "
                f"p97.5 {s['random_p975']:+.3f} → beats {s['beats']:.0f}% "
                f"(needs {BEAT_LINE:g}%) — {s['verdict']}")
    lines.append("per coin (both sides, net)")
    for sym, d in report["per_symbol"].items():
        s = d["all"]
        body = (f"n={s['n']} wr={s['win_rate']:.0f}% net {s['net']:+.3f} total {s['total']:+.1f}R"
                if s.get("n") else "no trades")
        lines.append(f"  {sym:<9} since {d['from']}  {body}")
    lines.append("per year (both sides, net)")
    for y, s in report["years"].items():
        lines.append(f"  {y}  n={s['n']:<4} net {s['net']:+.3f} total {s['total']:+.1f}R")
    lines.append("note     rules fixed before reading, not tuned; coins overlap in time, so "
                 "n overstates the evidence. Offline only — changes no signal.")
    return "\n".join(lines)
