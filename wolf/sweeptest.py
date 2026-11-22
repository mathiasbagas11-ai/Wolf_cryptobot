"""Offline test of a trend-pullback sweep entry (``/sweeptest``).

The owner sent a screenshot from Threads of someone's AI-assisted plan on
BTC. Daily below its EMAs and H4 making lower highs, so only shorts. Wait
for the H1 bounce to sweep the last swing high and close back under it,
short there, stop above the sweep, target the last H1 swing low. One chart
proves nothing, so the plan is turned into fixed rules and run on the basket.
Rules fixed before any read (registry ``trend-pullback-sweep``):

* **Trend:** the last completed daily close below its EMA50 and EMA200, and
  the last completed 4h close below its EMA50 → shorts only. The mirror
  (above all three) → longs only. Otherwise no trade. Daily and 4h bars
  are built from the 1h bars and only used once complete.
* **Swings:** an H1 fractal, a high above the 3 bars either side. It is
  usable only from the bar that confirms it (3 bars later).
* **Signal (short):** an H1 bar trades above the last confirmed swing high
  and closes back below it. Each swing is swept once. Entry at the next
  bar's open.
* **Stop:** the sweep bar's high + 0.25 × ATR(14, 1h). **Target:** the last
  confirmed H1 swing low. Skipped if it is not at least 1R away (a trader
  would not take it). Out at the close after ``HOLD_BARS`` (5 days). Stop
  first on a straddling bar. One position per coin. Longs mirror all of it.
* 20bps per round trip, charged on each trade's own stop.

Baseline: the whole trade set moved by one random number of whole days
(the ``/ichitest`` shift), keeping side, stop %, target multiple and time
cap. So clustering, geometry and hour survive and only the timing moves. The
bar is 97.5% plus net > 0 in both halves of the sample (split at the median
trade date). Nothing the bot emits changes.
"""

from __future__ import annotations

import logging
import random
import statistics
import time
from typing import Optional, Sequence

import requests

from wolf.bbtest import DEFAULT_SYMBOLS, _atr_series
from wolf.ewtest import simulate
from wolf.models import Candle

log = logging.getLogger("wolf.sweeptest")

H1_MS = 3_600_000
H4_MS = 4 * H1_MS
D1_MS = 24 * H1_MS
MAX_BARS = 24_000          # ~2.7 years of 1h bars
PIVOT = 3
STOP_ATR = 0.25
MIN_RR = 1.0
HOLD_BARS = 120            # 5 days
RANDOM_DRAWS = 300
BEAT_LINE = 97.5
MIN_TRADES = 100
_HOSTS = ("https://data-api.binance.vision/api/v3/klines", "https://api.binance.com/api/v3/klines")
_CACHE: dict[str, tuple[float, list[Candle]]] = {}


def fetch_1h(symbol: str, client=None, max_bars: int = MAX_BARS, timeout: float = 10.0) -> tuple[list[Candle], str]:
    hit = _CACHE.get(symbol)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1], "binance (cached)"
    for url in _HOSTS:
        out: list[Candle] = []
        end = None
        try:
            while len(out) < max_bars:
                params = {"symbol": symbol, "interval": "1h", "limit": 1000}
                if end is not None:
                    params["endTime"] = end
                resp = requests.get(url, params=params, timeout=timeout)
                if resp.status_code != 200:
                    break
                rows = resp.json()
                if not isinstance(rows, list) or not rows:
                    break
                page = [Candle.from_binance(r) for r in rows]
                out = page + out
                end = page[0].time - 1
                if len(rows) < 1000:
                    break
        except (requests.RequestException, ValueError, TypeError, IndexError):
            log.debug("sweeptest 1h via %s failed for %s", url, symbol, exc_info=True)
            continue
        now_ms = int(time.time() * 1000)
        seen, clean = set(), []
        for c in out:
            if c.time in seen or c.time + H1_MS > now_ms:
                continue
            seen.add(c.time)
            clean.append(c)
        if clean:
            clean = clean[-max_bars:]
            _CACHE[symbol] = (time.time(), clean)
            return clean, "binance"
    if client is not None:
        try:
            got = client.get_klines(symbol, "1h", 1000) or []
            if got:
                return got, "fallback (last 1000 bars only)"
        except Exception:
            log.debug("sweeptest fallback failed for %s", symbol, exc_info=True)
    return [], "unavailable"


def _ema(values: Sequence[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    k = 2 / (n + 1)
    e = None
    for i, v in enumerate(values):
        e = v if e is None else e + k * (v - e)
        out.append(e if i + 1 >= n else None)
    return out


def _htf(candles: Sequence[Candle], span: int) -> list[tuple[int, float]]:
    """(end time, close) of each complete higher-timeframe bar."""
    groups: dict[int, list[Candle]] = {}
    for c in candles:
        groups.setdefault(c.time // span, []).append(c)
    out = []
    per = span // H1_MS
    for g in sorted(groups):
        bars = groups[g]
        if len(bars) == per:
            out.append(((g + 1) * span, max(bars, key=lambda b: b.time).close))
    return out


def trend_by_bar(candles: Sequence[Candle]) -> list[int]:
    """+1 / −1 / 0 per 1h bar, from higher-timeframe bars complete at its close."""
    d1 = _htf(candles, D1_MS)
    h4 = _htf(candles, H4_MS)
    d50, d200 = _ema([c for _, c in d1], 50), _ema([c for _, c in d1], 200)
    h50 = _ema([c for _, c in h4], 50)
    out, di, hi = [], -1, -1
    for c in candles:
        close_t = c.time + H1_MS
        while di + 1 < len(d1) and d1[di + 1][0] <= close_t:
            di += 1
        while hi + 1 < len(h4) and h4[hi + 1][0] <= close_t:
            hi += 1
        if di < 0 or hi < 0 or d200[di] is None or d50[di] is None or h50[hi] is None:
            out.append(0)
            continue
        dc, hc = d1[di][1], h4[hi][1]
        if dc < d50[di] and dc < d200[di] and hc < h50[hi]:
            out.append(-1)
        elif dc > d50[di] and dc > d200[di] and hc > h50[hi]:
            out.append(1)
        else:
            out.append(0)
    return out


def run_rule(candles: Sequence[Candle]) -> list[dict]:
    trend = trend_by_bar(candles)
    atrs = _atr_series(candles)
    trades, free_from = [], 0
    sh = sl = None              # (price, index) of the last confirmed swing high / low
    swept_h = swept_l = None
    n = len(candles)
    for i in range(n - 1):
        j = i - PIVOT           # the bar that becomes confirmable at i
        if j >= PIVOT:
            win = candles[j - PIVOT:j + PIVOT + 1]
            if candles[j].high == max(b.high for b in win) and \
                    all(candles[j].high > b.high for b in candles[j - PIVOT:j]):
                sh = (candles[j].high, j)
            if candles[j].low == min(b.low for b in win) and \
                    all(candles[j].low < b.low for b in candles[j - PIVOT:j]):
                sl = (candles[j].low, j)
        b = candles[i]
        atr = atrs[i]
        if i + 1 < free_from or atr != atr or atr <= 0 or trend[i] == 0:
            continue
        k = i + 1
        entry = candles[k].open
        if trend[i] < 0 and sh and sl and swept_h != sh[1] and b.high > sh[0] and b.close < sh[0]:
            swept_h = sh[1]
            stop = b.high + STOP_ATR * atr
            target = sl[0]
            long = False
        elif trend[i] > 0 and sh and sl and swept_l != sl[1] and b.low < sl[0] and b.close > sl[0]:
            swept_l = sl[1]
            stop = b.low - STOP_ATR * atr
            target = sh[0]
            long = True
        else:
            continue
        risk = (entry - stop) if long else (stop - entry)
        reward = (target - entry) if long else (entry - target)
        if risk <= 0 or reward < MIN_RR * risk:
            continue
        res = simulate(candles, k, entry, stop, target, long, hold=HOLD_BARS)
        if res is None:
            continue
        r, exit_i, how = res
        trades.append({"r": r, "how": how, "long": long, "bar": k, "time": candles[k].time,
                       "risk_pct": risk / entry * 100, "target_r": reward / risk})
        free_from = exit_i + 1
    return trades


def shift_baseline(series: dict, trades: dict, cost_bps: float, draws: int = RANDOM_DRAWS,
                   seed: int = 43) -> list[float]:
    rng = random.Random(seed)
    span = min(len(c) for c in series.values()) - HOLD_BARS - 1
    out: list[float] = []
    if span <= 48:
        return out
    for _ in range(draws):
        days = rng.randrange(1, span // 24)
        rs = []
        for sym, ts in trades.items():
            candles = series[sym]
            usable = len(candles) - HOLD_BARS - 1
            for t in ts:
                k = 1 + (t["bar"] - 1 + days * 24) % usable
                entry = candles[k].open
                f = t["risk_pct"] / 100
                stop = entry * (1 - f) if t["long"] else entry * (1 + f)
                tgt = entry * (1 + t["target_r"] * f) if t["long"] else entry * (1 - t["target_r"] * f)
                res = simulate(candles, k, entry, stop, tgt, t["long"], hold=HOLD_BARS)
                if res is not None:
                    rs.append(res[0] - (cost_bps / 100.0) / t["risk_pct"])
        if rs:
            out.append(statistics.fmean(rs))
    return out


def _net(t: dict, cost_bps: float) -> float:
    return t["r"] - (cost_bps / 100.0) / t["risk_pct"]


def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None, draws: int = RANDOM_DRAWS) -> dict:
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, src = fetch_1h(sym, client)
            sources[sym] = src
            if len(candles) > 24 * 260:
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources}
    if not series:
        report["error"] = "no 1h history could be fetched for any symbol"
        return report
    report["from"] = time.strftime("%Y-%m-%d", time.gmtime(min(c[0].time for c in series.values()) / 1000))
    trades = {sym: run_rule(c) for sym, c in series.items()}
    every = [t for ts in trades.values() for t in ts]
    if not every:
        report["error"] = "no trades"
        return report
    nets = [_net(t, cost_bps) for t in every]
    mid = statistics.median(t["time"] for t in every)
    first = [x for x, t in zip(nets, every) if t["time"] < mid]
    second = [x for x, t in zip(nets, every) if t["time"] >= mid]
    by = lambda flag: [x for x, t in zip(nets, every) if t["long"] is flag]  # noqa: E731
    r = {
        "n": len(every), "win": round(sum(x > 0 for x in nets) / len(nets) * 100, 1),
        "gross": round(statistics.fmean(t["r"] for t in every), 4), "net": round(statistics.fmean(nets), 4),
        "total": round(sum(nets), 1), "risk_pct": round(statistics.median(t["risk_pct"] for t in every), 2),
        "target_r": round(statistics.median(t["target_r"] for t in every), 2),
        "how": {h: sum(1 for t in every if t["how"] == h) for h in ("target", "stop", "timeout")},
        "long_n": len(by(True)), "long": round(statistics.fmean(by(True)), 4) if by(True) else None,
        "short_n": len(by(False)), "short": round(statistics.fmean(by(False)), 4) if by(False) else None,
        "split": time.strftime("%Y-%m-%d", time.gmtime(mid / 1000)),
        "first": round(statistics.fmean(first), 4) if first else None,
        "second": round(statistics.fmean(second), 4) if second else None,
        "per_symbol": {s: round(statistics.fmean(_net(t, cost_bps) for t in ts), 3)
                       for s, ts in trades.items() if ts},
    }
    base = shift_baseline(series, trades, cost_bps, draws=draws)
    if base:
        srt = sorted(base)
        r["random_median"] = round(statistics.median(srt), 4)
        r["random_hi"] = round(srt[int(BEAT_LINE / 100 * (len(srt) - 1))], 4)
        r["beats"] = round(sum(b < r["net"] for b in srt) / len(srt) * 100, 1)
        halves = r["first"] is not None and r["second"] is not None and r["first"] > 0 and r["second"] > 0
        if r["n"] < MIN_TRADES:
            r["verdict"] = f"INCONCLUSIVE (n<{MIN_TRADES})"
        elif r["beats"] >= BEAT_LINE and r["net"] > 0 and halves:
            r["verdict"] = "PASSES the pre-registered bar — still one backtest, not a strategy"
        else:
            r["verdict"] = "does not beat the same trades shifted to random days"
    report["result"] = r
    return report


def render(report: dict) -> str:
    head = (f"SWEEP-TEST | trend-pullback sweep (D1<EMA50&200, H4<EMA50 → shorts; mirror longs), "
            f"H1 sweep of last swing, TP last opposite swing (≥{MIN_RR:g}R), net of {report['cost_bps']:g}bps")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head, f"since    {report['from']} (1h bars)"]
    short_hist = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    if short_hist:
        lines.append(f"WARNING  only ~6 weeks of 1h history for {', '.join(short_hist)}")
    r = report["result"]
    lines.append(f"ALL      n={r['n']} win {r['win']:.0f}% · gross {r['gross']:+.3f}R · net {r['net']:+.3f}R · "
                 f"total {r['total']:+.1f}R · 1R {r['risk_pct']}% · target median {r['target_r']}R")
    lines.append(f"         target {r['how']['target']} · stop {r['how']['stop']} · 5-day cap {r['how']['timeout']}")
    ln = "n/a" if r["long"] is None else f"{r['long']:+.3f}"
    sn = "n/a" if r["short"] is None else f"{r['short']:+.3f}"
    lines.append(f"         LONG n={r['long_n']} {ln} · SHORT n={r['short_n']} {sn}")
    fh = "n/a" if r["first"] is None else f"{r['first']:+.3f}"
    sh = "n/a" if r["second"] is None else f"{r['second']:+.3f}"
    lines.append(f"         first half (to {r['split']}): {fh} | second half: {sh}")
    if "beats" in r:
        lines.append(f"         same trades on random days: median {r['random_median']:+.3f}, p{BEAT_LINE:g} "
                     f"{r['random_hi']:+.3f} → beats {r['beats']:.0f}% (needs {BEAT_LINE:g}% and both halves > 0) "
                     f"— {r['verdict']}")
    per = sorted(r["per_symbol"].items(), key=lambda kv: kv[1])
    lines.append("         per coin net: " + ", ".join(f"{s.replace('USDT', '')} {v:+.2f}" for s, v in per))
    lines.append("note     rules fixed before reading; the screenshot's AI plan is one discretionary example, "
                 "this is its mechanical core. Offline only — changes no signal.")
    return "\n".join(lines)
