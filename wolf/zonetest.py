"""Offline test of "daily high/low zones" (``/zonetest``).

The owner sent a TikTok (@chebito87) selling an indicator that draws a daily
high zone and a daily low zone at 00:00, before the session. It claims a
"90% winrate over 4 years", and shows that ~88% of days land within ±0.25
ADR20 of its forecast. The 90% is coverage (the day's extreme fell inside the
zone), not a trade win rate. The formula is not published, so this tests two
things that do not need it:

1. **Coverage of a naive zone.** How often does a zone built from nothing but
   the day's open and ADR20 contain the day's high (low)? The zone centre is
   the open plus the median of the last 60 days' (high − open) / ADR20
   (mirror for the low). That says whether 85–90% is hard to reach or comes
   almost free. It also prints the share of days within ±0.10 / ±0.25 /
   ±0.50 ADR of the centre, the same axis as the video's last chart.
2. **The trade the video implies.** Fade the zone: short at the bottom edge of
   the high zone, long at the top edge of the low zone, on first touch. The
   stop is at the far edge of the zone and the target is the day's open. Out
   at the day's last 4h close otherwise. Zone half-width 0.25 ADR20, so the
   risk is 0.5 ADR20. Stop first on a straddling bar, and the target never
   fills on the entry bar. 20bps per round trip.

Baseline for (2): the same coins, days and geometry, but each day's zone
centre is taken from another random day of the same coin. The levels stay
realistic; only the day-specific placement is random, which is the one thing
a forecast claims. Bars are 4h, so intraday order is coarse, and the coarse
side is charged against the trade.

Rules and the bar are in the registry (``daily-zone-fade``), fixed before any
read. Nothing the bot emits changes.
"""

from __future__ import annotations

import random
import statistics
import time
from typing import Optional, Sequence

from wolf.bbtest import DEFAULT_SYMBOLS, fetch_history
from wolf.models import Candle

DAY_MS = 86_400_000
ADR_DAYS = 20
CENTRE_DAYS = 60
HALF_WIDTH = 0.25          # zone half-width in ADR20
RANDOM_DRAWS = 300
BEAT_LINE = 97.5
MIN_TRADES = 100
SPLIT_YEAR = 2024


def daily_bars(candles: Sequence[Candle]) -> list[dict]:
    """UTC days built from 4h bars; only complete days (6 bars) are kept."""
    days: dict[int, list[Candle]] = {}
    for c in candles:
        days.setdefault(c.time // DAY_MS, []).append(c)
    out = []
    for d in sorted(days):
        bars = sorted(days[d], key=lambda b: b.time)
        if len(bars) != 6:
            continue
        out.append({"day": d, "bars": bars, "open": bars[0].open, "close": bars[-1].close,
                    "high": max(b.high for b in bars), "low": min(b.low for b in bars)})
    return out


def zones(days: list[dict]) -> list[Optional[dict]]:
    """Per day, the naive zones known at 00:00 (prior days only)."""
    rng = [x["high"] - x["low"] for x in days]
    adr = [None] * len(days)                     # adr[i] = mean range of days i-20 .. i-1
    for i in range(ADR_DAYS, len(days)):
        adr[i] = sum(rng[i - ADR_DAYS:i]) / ADR_DAYS
    up = [None] * len(days)
    dn = [None] * len(days)
    for j, d in enumerate(days):
        if adr[j]:
            up[j] = (d["high"] - d["open"]) / adr[j]
            dn[j] = (d["open"] - d["low"]) / adr[j]
    out: list[Optional[dict]] = []
    for i in range(len(days)):
        if i < CENTRE_DAYS + ADR_DAYS or not adr[i]:
            out.append(None)
            continue
        # each past day's ratio uses that day's own (causal) ADR20
        u = [x for x in up[i - CENTRE_DAYS:i] if x is not None]
        v = [x for x in dn[i - CENTRE_DAYS:i] if x is not None]
        if not u or not v:
            out.append(None)
            continue
        out.append({"adr": adr[i], "k_up": statistics.median(u), "k_dn": statistics.median(v)})
    return out


def coverage(days: list[dict], zs: list[Optional[dict]]) -> dict:
    hits = {"high": 0, "low": 0, "both": 0}
    err = {"high": [], "low": []}
    n = 0
    for d, z in zip(days, zs):
        if z is None:
            continue
        n += 1
        ch = d["open"] + z["k_up"] * z["adr"]
        cl = d["open"] - z["k_dn"] * z["adr"]
        eh = abs(d["high"] - ch) / z["adr"]
        el = abs(d["low"] - cl) / z["adr"]
        err["high"].append(eh)
        err["low"].append(el)
        h, l = eh <= HALF_WIDTH, el <= HALF_WIDTH
        hits["high"] += h
        hits["low"] += l
        hits["both"] += h and l
    if not n:
        return {"days": 0}
    within = {side: {str(t): round(sum(e <= t for e in err[side]) / n * 100, 1) for t in (0.10, 0.25, 0.50)}
              for side in ("high", "low")}
    return {"days": n, "high": round(hits["high"] / n * 100, 1), "low": round(hits["low"] / n * 100, 1),
            "both": round(hits["both"] / n * 100, 1), "within": within}


def fade_trades(day: dict, adr: float, k_up: float, k_dn: float) -> list[dict]:
    """The zone fade on one day: at most one short and one long, first touch."""
    out = []
    o = day["open"]
    for short in (True, False):
        k = k_up if short else k_dn
        if k - HALF_WIDTH <= 0:
            continue                                   # the entry edge sits at or past the open
        s = -1 if short else 1                         # +1 long
        entry = o - s * (k - HALF_WIDTH) * adr         # near edge of the zone
        stop = o - s * (k + HALF_WIDTH) * adr          # far edge
        risk = abs(stop - entry)
        filled = False
        res = None
        for j, b in enumerate(day["bars"]):
            touch = (b.high >= entry) if short else (b.low <= entry)
            if not filled:
                if not touch:
                    continue
                filled = True
                fill = max(entry, b.open) if short else min(entry, b.open)   # gap gives a better fill
                if (b.high >= stop) if short else (b.low <= stop):
                    px = stop if not ((b.open >= stop) if short else (b.open <= stop)) else b.open
                    res = (fill - px) / risk if short else (px - fill) / risk
                    break
                continue                               # target never on the entry bar
            if (b.high >= stop) if short else (b.low <= stop):
                px = max(stop, b.open) if short else min(stop, b.open)
                res = (fill - px) / risk if short else (px - fill) / risk
                break
            if (b.low <= o) if short else (b.high >= o):
                px = min(o, b.open) if short else max(o, b.open)
                res = (fill - px) / risk if short else (px - fill) / risk
                break
        if filled and res is None:
            last = day["bars"][-1].close
            res = (fill - last) / risk if short else (last - fill) / risk
        if filled:
            out.append({"r": res, "short": short, "risk_pct": risk / fill * 100, "day": day["day"]})
    return out


def _net(t: dict, cost_bps: float) -> float:
    return t["r"] - (cost_bps / 100.0) / t["risk_pct"]


def _year(day: int) -> int:
    return time.gmtime(day * 86_400).tm_year


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None, draws: int = RANDOM_DRAWS, seed: int = 41) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch_history(sym, client)
            sources[sym] = source
            if len(candles) > 6 * (CENTRE_DAYS + ADR_DAYS + 30):
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources}
    if not series:
        report["error"] = "no candle history could be fetched for any symbol"
        return report
    per = {}
    for sym, candles in series.items():
        ds = daily_bars(candles)
        per[sym] = (ds, zones(ds))
    first = min((ds[0]["day"] for ds, _ in per.values() if ds), default=0)
    report["from"] = time.strftime("%Y-%m-%d", time.gmtime(first * 86_400))

    # 1. coverage, pooled over coins
    covs = [coverage(ds, zs) for ds, zs in per.values()]
    n = sum(c["days"] for c in covs)
    if n:
        w = lambda key: round(sum(c[key] * c["days"] for c in covs if c["days"]) / n, 1)  # noqa: E731
        report["coverage"] = {
            "days": n, "high": w("high"), "low": w("low"), "both": w("both"),
            "within": {side: {t: round(sum(c["within"][side][t] * c["days"] for c in covs if c["days"]) / n, 1)
                              for t in ("0.1", "0.25", "0.5")} for side in ("high", "low")},
        }

    # 2. the fade
    trades = []
    for ds, zs in per.values():
        for d, z in zip(ds, zs):
            if z is not None:
                trades += fade_trades(d, z["adr"], z["k_up"], z["k_dn"])
    if not trades:
        report["error"] = "no trades"
        return report
    nets = [_net(t, cost_bps) for t in trades]
    before = [x for x, t in zip(nets, trades) if _year(t["day"]) < SPLIT_YEAR]
    after = [x for x, t in zip(nets, trades) if _year(t["day"]) >= SPLIT_YEAR]
    fade = {"n": len(trades), "win": round(sum(x > 0 for x in nets) / len(nets) * 100, 1),
            "gross": round(statistics.fmean(t["r"] for t in trades), 4),
            "net": round(statistics.fmean(nets), 4),
            "risk_pct": round(statistics.median(t["risk_pct"] for t in trades), 2),
            "short": round(statistics.fmean(x for x, t in zip(nets, trades) if t["short"]), 4)
            if any(t["short"] for t in trades) else None,
            "long": round(statistics.fmean(x for x, t in zip(nets, trades) if not t["short"]), 4)
            if any(not t["short"] for t in trades) else None,
            "before": round(statistics.fmean(before), 4) if before else None,
            "after": round(statistics.fmean(after), 4) if after else None}

    # baseline: each day's centre offsets come from a random other day of the same coin
    rng = random.Random(seed)
    base = []
    for _ in range(draws):
        rs = []
        for ds, zs in per.values():
            valid = [z for z in zs if z is not None]
            if not valid:
                continue
            for d, z in zip(ds, zs):
                if z is None:
                    continue
                other = rng.choice(valid)
                for t in fade_trades(d, z["adr"], other["k_up"], other["k_dn"]):
                    rs.append(_net(t, cost_bps))
        if rs:
            base.append(statistics.fmean(rs))
    if base:
        srt = sorted(base)
        fade["random_median"] = round(statistics.median(srt), 4)
        fade["random_hi"] = round(srt[int(BEAT_LINE / 100 * (len(srt) - 1))], 4)
        fade["beats"] = round(sum(b < fade["net"] for b in srt) / len(srt) * 100, 1)
        halves_ok = fade["before"] is not None and fade["after"] is not None \
            and fade["before"] > 0 and fade["after"] > 0
        if fade["n"] < MIN_TRADES:
            fade["verdict"] = f"INCONCLUSIVE (n<{MIN_TRADES})"
        elif fade["beats"] >= BEAT_LINE and fade["net"] > 0 and halves_ok:
            fade["verdict"] = "PASSES the pre-registered bar — still one backtest, not a strategy"
        else:
            fade["verdict"] = "does not beat zones placed on random days"
    report["fade"] = fade
    return report


def render(report: dict) -> str:
    head = (f"ZONE-TEST | naive daily high/low zones (open + median offset, ±{HALF_WIDTH:g} ADR20), "
            f"10 coins, 4h bars, net of {report['cost_bps']:g}bps")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head, f"since    {report['from']} (out-of-sample cut: {SPLIT_YEAR})"]
    short_hist = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    if short_hist:
        lines.append(f"WARNING  only ~5.5 months of history for {', '.join(short_hist)}")
    c = report.get("coverage")
    if c:
        lines.append(f"COVERAGE {c['days']} coin-days · high inside zone {c['high']:.0f}% · low {c['low']:.0f}% · "
                     f"both {c['both']:.0f}%  (video claims ~85–90%)")
        wh, wl = c["within"]["high"], c["within"]["low"]
        lines.append(f"         share within ±0.10 / ±0.25 / ±0.50 ADR of centre: high {wh['0.1']:.0f}/"
                     f"{wh['0.25']:.0f}/{wh['0.5']:.0f}% · low {wl['0.1']:.0f}/{wl['0.25']:.0f}/{wl['0.5']:.0f}%"
                     "  (video: ~88% at ±0.25)")
    f = report["fade"]
    lines.append(f"FADE     n={f['n']} win {f['win']:.0f}% · gross {f['gross']:+.3f}R · net {f['net']:+.3f}R · "
                 f"1R {f['risk_pct']}% · SHORT {f['short']:+.3f} · LONG {f['long']:+.3f}")
    bf = "n/a" if f["before"] is None else f"{f['before']:+.3f}"
    af = "n/a" if f["after"] is None else f"{f['after']:+.3f}"
    lines.append(f"         before {SPLIT_YEAR}: {bf} | {SPLIT_YEAR}+: {af}")
    if "beats" in f:
        lines.append(f"         zones placed on random days: median {f['random_median']:+.3f}, p{BEAT_LINE:g} "
                     f"{f['random_hi']:+.3f} → beats {f['beats']:.0f}% (needs {BEAT_LINE:g}% and both halves > 0) "
                     f"— {f['verdict']}")
    lines.append("note     coverage is not a win rate. 4h bars: intraday order charged against the trade. "
                 "Offline only — changes no signal.")
    return "\n".join(lines)
