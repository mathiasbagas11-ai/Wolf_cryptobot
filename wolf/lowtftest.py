"""Offline backtest of v2's rule on a lower timeframe (``/trendtest 1h|15m``).

The owner asked whether v2 (``/trendtest`` candidate A, now running forward
on 4h paper) could be moved to a lower timeframe "for scalping", as a v3. The
live v2 is not touched: this answers the question offline first, because a
forward paper run would take months to say what history can say today.

The rule is A's, transplanted bar for bar — the same bar counts on a shorter
clock, nothing re-chosen:
* BTC regime: the close against its own 300-bar mean on this timeframe.
* Entry: a close beyond the previous 20 bars' extreme in the regime's
  direction, filled at the next open; one position per coin.
* Stop 2×ATR(14), trailing 3×ATR from the best price, out after 90 bars.
These are ``wolf.trendtest``'s own functions (``btc_regime``, ``run_trend``,
``simulate_trail``), called, not copied.

Same bar counts, not the same calendar spans, because "v2 on 15m" means the
same chart reading on a 15m chart; at 15m the regime looks ~3 days back and a
trade lasts at most ~22 hours.

The prediction written before any read (registry
``v2-rule-on-lower-timeframes``): cost scales with 1/ATR, and ATR shrinks
roughly with the square root of the bar length, so the ~0.05R a trade cost
at 4h becomes ~0.10R at 1h and ~0.20R at 15m, while nothing in this
project's lower-timeframe record shows gross edge growing to pay it. Expected:
15m fails; 1h most likely fails.

Baseline as in ``/trendtest``: the whole trade set moved by one random number
of whole days, so the clustering across coins, each trade's side, stop
distance and UTC hour survive and only the timing moves. Two timeframes are
two tests, so each must beat 97.5%, be net positive, and be net positive in
both halves split at the median trade date (the 15m history is too short for
a calendar cut). Funding is not modelled.
"""

from __future__ import annotations

import logging
import random
import statistics
import time
from typing import Optional, Sequence

import requests

from wolf.bbtest import DEFAULT_SYMBOLS
from wolf.models import Candle
from wolf.trendtest import (BREAKOUT_BARS, HOLD_BARS, REGIME_BARS, STOP_ATR, TRAIL_MULT,
                            btc_regime, run_trend, simulate_trail)

log = logging.getLogger("wolf.lowtftest")

# timeframe -> (bar ms, bars per day, history cap)
FRAMES = {
    "1h": (3_600_000, 24, 24_000),     # ~2.7 years
    "15m": (900_000, 96, 35_000),      # ~1 year
}
RANDOM_DRAWS = 200
BEAT_LINE = 97.5
MIN_TRADES = 100
_HOSTS = ("https://data-api.binance.vision/api/v3/klines", "https://api.binance.com/api/v3/klines")
_CACHE: dict[tuple[str, str], tuple[float, list[Candle]]] = {}


def fetch(symbol: str, tf: str, client=None, timeout: float = 10.0) -> tuple[list[Candle], str]:
    """Closed bars of ``tf``, paged back from now to the frame's history cap."""
    bar_ms, _, cap = FRAMES[tf]
    hit = _CACHE.get((symbol, tf))
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1], "binance (cached)"
    for url in _HOSTS:
        out: list[Candle] = []
        end = None
        try:
            while len(out) < cap:
                params = {"symbol": symbol, "interval": tf, "limit": 1000}
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
            log.debug("lowtftest %s via %s failed for %s", tf, url, symbol, exc_info=True)
            continue
        now_ms = int(time.time() * 1000)
        seen, clean = set(), []
        for c in out:
            if c.time in seen or c.time + bar_ms > now_ms:
                continue
            seen.add(c.time)
            clean.append(c)
        if clean:
            clean = clean[-cap:]
            _CACHE[(symbol, tf)] = (time.time(), clean)
            return clean, "binance"
    if client is not None:
        try:
            got = client.get_klines(symbol, tf, 1000) or []
            if got:
                return got, "fallback (last 1000 bars only)"
        except Exception:
            log.debug("lowtftest fallback failed for %s", symbol, exc_info=True)
    return [], "unavailable"


def shift_baseline(series: dict, trades: dict, cost_bps: float, day_bars: int,
                   draws: int = RANDOM_DRAWS, seed: int = 47) -> list[float]:
    """Mean net R per draw with every trade moved by one random number of days."""
    rng = random.Random(seed)
    span = min(len(c) for c in series.values()) - HOLD_BARS - 1
    out: list[float] = []
    if span <= day_bars * 2:
        return out
    for _ in range(draws):
        days = rng.randrange(1, span // day_bars)
        rs = []
        for sym, ts in trades.items():
            candles = series[sym]
            usable = len(candles) - HOLD_BARS - 1
            for t in ts:
                k = 1 + (t["bar"] - 1 + days * day_bars) % usable
                entry = candles[k].open
                res = simulate_trail(candles, k, entry * t["risk_pct"] / 100, t["long"])
                if res is not None:
                    rs.append(res[0] - (cost_bps / 100.0) / t["risk_pct"])
        if rs:
            out.append(statistics.fmean(rs))
    return out


def _net(t: dict, cost_bps: float) -> float:
    return t["r"] - (cost_bps / 100.0) / t["risk_pct"]


def _summary(rows: Sequence[dict], cost_bps: float) -> dict:
    if not rows:
        return {"n": 0}
    net = [_net(t, cost_bps) for t in rows]
    return {"n": len(rows), "win_rate": round(sum(r > 0 for r in net) / len(net) * 100, 1),
            "gross": round(statistics.fmean(t["r"] for t in rows), 3),
            "net": round(statistics.fmean(net), 3), "total": round(sum(net), 1),
            "cost": round(statistics.fmean((cost_bps / 100.0) / t["risk_pct"] for t in rows), 3),
            "risk_pct": round(statistics.median(t["risk_pct"] for t in rows), 2)}


def backtest(tf: str, symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None, cost_bps: float = 20.0,
             series: Optional[dict] = None, draws: int = RANDOM_DRAWS) -> dict:
    if tf not in FRAMES:
        return {"tf": tf, "cost_bps": cost_bps, "error": f"timeframe must be one of {', '.join(FRAMES)}"}
    _, day_bars, _ = FRAMES[tf]
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch(sym, tf, client)
            sources[sym] = source
            if len(candles) > REGIME_BARS + HOLD_BARS + 50:
                series[sym] = candles
    report: dict = {"tf": tf, "cost_bps": cost_bps, "sources": sources}
    btc = series.get("BTCUSDT")
    if not series or btc is None:
        report["error"] = "no BTC history on this timeframe, so there is no regime to read"
        return report
    report["from"] = time.strftime("%Y-%m-%d", time.gmtime(min(c[0].time for c in series.values()) / 1000))

    regime = btc_regime(btc)
    trades = {sym: run_trend(c, regime) for sym, c in series.items()}
    every = sorted((t for ts in trades.values() for t in ts), key=lambda t: t["time"])
    a = _summary(every, cost_bps)
    if a["n"]:
        mid = every[len(every) // 2]["time"]
        a["split"] = time.strftime("%Y-%m-%d", time.gmtime(mid / 1000))
        a["long"] = _summary([t for t in every if t["long"]], cost_bps)
        a["short"] = _summary([t for t in every if not t["long"]], cost_bps)
        a["first"] = _summary([t for t in every if t["time"] < mid], cost_bps)
        a["second"] = _summary([t for t in every if t["time"] >= mid], cost_bps)
        a["per_symbol"] = {s: _summary(ts, cost_bps) for s, ts in trades.items()}
        base = shift_baseline(series, trades, cost_bps, day_bars, draws=draws)
        if base:
            srt = sorted(base)
            a["median"] = round(statistics.median(srt), 4)
            a["hi"] = round(srt[int(BEAT_LINE / 100 * (len(srt) - 1))], 4)
            a["beats"] = round(sum(1 for b in srt if b < a["net"]) / len(srt) * 100, 1)
            halves = [a["first"].get("net"), a["second"].get("net")]
            if a["n"] < MIN_TRADES:
                a["verdict"] = f"INCONCLUSIVE (n<{MIN_TRADES})"
            elif a["beats"] >= BEAT_LINE and a["net"] > 0 and all(h is not None and h > 0 for h in halves):
                a["verdict"] = "PASSES the pre-registered bar — earns a v3 paper run, not real money"
            else:
                a["verdict"] = "does not beat the same trades shifted to random days"
    report["trend"] = a
    return report


def render(report: dict) -> str:
    tf = report.get("tf", "?")
    head = (f"V2-ON-{tf.upper()} | v2's rule bar for bar on {tf}: BTC regime {REGIME_BARS}-bar mean, "
            f"{BREAKOUT_BARS}-bar breakout with it, stop {STOP_ATR:g}xATR, trail {STOP_ATR * TRAIL_MULT:g}xATR, "
            f"out after {HOLD_BARS} bars; net of {report.get('cost_bps', 20):g}bps, funding not modelled")
    if "error" in report:
        return head + "\n" + report["error"]
    lines = [head, f"since    {report['from']} ({tf} bars)"]
    short_hist = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    missing = [s for s, src in report["sources"].items() if src == "unavailable"]
    if short_hist:
        lines.append(f"WARNING  only the last 1000 bars for {', '.join(short_hist)}")
    if missing:
        lines.append(f"skipped  no data: {', '.join(missing)}")
    a = report.get("trend") or {}
    if not a.get("n"):
        lines.append("ALL      no trades")
        return "\n".join(lines)
    lines.append(f"ALL      n={a['n']} win {a['win_rate']:.0f}% · gross {a['gross']:+.3f}R · cost {a['cost']:.3f}R · "
                 f"net {a['net']:+.3f}R · total {a['total']:+.1f}R · 1R {a['risk_pct']}%")
    side = lambda s: f"n={s['n']} {s['net']:+.3f}" if s.get("n") else "no trades"  # noqa: E731
    lines.append(f"         LONG {side(a['long'])} · SHORT {side(a['short'])}")
    lines.append(f"         first half (to {a['split']}): {side(a['first'])} | second half: {side(a['second'])}")
    if "beats" in a:
        lines.append(f"         same trades on random days: median {a['median']:+.3f}, p{BEAT_LINE:g} {a['hi']:+.3f} "
                     f"→ beats {a['beats']:.0f}% (needs {BEAT_LINE:g}% and both halves > 0) — {a['verdict']}")
    per = sorted(((s, d) for s, d in a["per_symbol"].items() if d.get("n")), key=lambda kv: kv[1]["net"])
    lines.append("         per coin net: " + ", ".join(f"{s.replace('USDT', '')} {d['net']:+.2f}" for s, d in per))
    lines.append("compare  v2 on 4h (/trendtest A): gross +0.131 · cost ~0.05 · net +0.080, beats 95%")
    lines.append("note     rules are v2's, not tuned; prediction written before the read: 15m fails, 1h likely "
                 "fails. Offline only — v2 and every live signal are unchanged.")
    return "\n".join(lines)
