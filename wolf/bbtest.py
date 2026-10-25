"""Backtest of a Bollinger-band dip-buying rule the owner brought from a video.

The video's claim: on BTCUSDT 4h over ~6 years, buying any touch of the lower
band (20, 2) lost (~30% win rate, -7.8R), while a refined rule won (~46%,
+52R, positive out of sample). The refined rule, as summarised for the owner:

* a green candle with a long body,
* whose low dips below the 1.5-deviation band but not through the 2.0 band,
* stop just below that candle's low, and out after at most 20 candles.

The rule is taken as stated and **not tuned here**. Its three numbers (1.5,
the body test, 20 bars) were chosen on the video's own data, so re-fitting
them on ours would only find a second set that flatters a second sample.
"Long body" is the one phrase the summary leaves vague; it is fixed below,
before any reading, as body >= half the candle's range and range >= ATR(14).

Two things the video's number cannot answer, and this card can:

* **Drift.** BTC rose roughly tenfold over the video's window, so almost
  any long-only rule looks good there. Every result is set against a random
  baseline: the same number of entries on random bars of the same coins,
  with the same stop rule and the same 20-bar exit. The rule has to beat
  that, not zero.
* **One coin.** The bot trades dozens of alts. The card runs a basket of
  majors and prints each one, plus every calendar year pooled.

Costs are charged per trade on the trade's own stop, as everywhere else.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
import time
from typing import Optional, Sequence

import requests

from wolf.models import Candle

log = logging.getLogger("wolf.bbtest")

PERIOD = 20
OUTER_K = 2.0
INNER_K = 1.5
HOLD_BARS = 20
INTERVAL = "4h"
BAR_MS = 4 * 3600 * 1000
DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
)
#: ~6 years of 4h bars, the video's window.
MAX_BARS = 13_200
RANDOM_DRAWS = 500

_BINANCE_HOSTS = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
)
_CACHE: dict[tuple[str, str], tuple[float, list[Candle]]] = {}
_CACHE_TTL_S = 6 * 3600


# ── data ────────────────────────────────────────────────────────────────────

def _page_binance(url: str, symbol: str, max_bars: int, timeout: float) -> list[Candle]:
    """Walk backwards from now in pages of 1000 until ``max_bars`` or listing."""
    out: list[Candle] = []
    end: Optional[int] = None
    while len(out) < max_bars:
        params = {"symbol": symbol, "interval": INTERVAL, "limit": 1000}
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
    # Pages meet at a boundary; drop any duplicate open times and the bar
    # still forming, which has not closed and would be read as if it had.
    seen, clean = set(), []
    now_ms = int(time.time() * 1000)
    for c in out:
        if c.time in seen or c.time + BAR_MS > now_ms:
            continue
        seen.add(c.time)
        clean.append(c)
    return clean[-max_bars:]


def fetch_history(symbol: str, client=None, max_bars: int = MAX_BARS,
                  timeout: float = 10.0) -> tuple[list[Candle], str]:
    """Long 4h history and where it came from.

    Binance's public data host serves years of history by paging on
    ``endTime``. When it cannot be reached, the bot's own multi-venue client
    still serves its last 1000 bars (~5.5 months), and the card says so
    rather than quietly reading a shorter window as the video's six years.
    """
    key = (symbol, INTERVAL)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1], "binance (cached)"
    for url in _BINANCE_HOSTS:
        try:
            candles = _page_binance(url, symbol, max_bars, timeout)
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            log.debug("bbtest history via %s failed for %s: %s", url, symbol, exc)
            continue
        if candles:
            _CACHE[key] = (time.time(), candles)
            return candles, "binance"
    if client is not None:
        try:
            candles = client.get_klines(symbol, INTERVAL, 1000)
        except Exception:
            log.debug("bbtest fallback klines failed for %s", symbol, exc_info=True)
            candles = []
        if candles:
            return candles, "fallback (last 1000 bars only)"
    return [], "unavailable"


# ── rule ────────────────────────────────────────────────────────────────────

def _bands(closes: Sequence[float], i: int) -> Optional[tuple[float, float, float]]:
    """(mid, lower 1.5, lower 2.0) from the PERIOD closes ending at bar i."""
    if i + 1 < PERIOD:
        return None
    window = closes[i + 1 - PERIOD: i + 1]
    mid = sum(window) / PERIOD
    sd = math.sqrt(sum((x - mid) ** 2 for x in window) / PERIOD)
    return mid, mid - INNER_K * sd, mid - OUTER_K * sd


def _atr_series(candles: Sequence[Candle], period: int = 14) -> list[float]:
    out = [float("nan")] * len(candles)
    trs: list[float] = []
    for i, c in enumerate(candles):
        prev = candles[i - 1].close if i else c.close
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
        if i + 1 >= period:
            out[i] = sum(trs[-period:]) / period
    return out


def is_naive_signal(c: Candle, bands) -> bool:
    """The video's losing rule: any touch of the 2.0 band."""
    return bands is not None and c.low <= bands[2]


def is_long_green(c: Candle, atr: float) -> bool:
    """The candle half of the refined rule: green, body >= half the range,
    range >= ATR(14). Kept separate because the baseline needs exactly this."""
    if math.isnan(atr):
        return False
    rng = c.high - c.low
    body = c.close - c.open
    return body > 0 and rng > 0 and body >= 0.5 * rng and rng >= atr


def is_refined_signal(c: Candle, bands, atr: float) -> bool:
    """The video's refined rule, with 'long body' fixed as documented above."""
    if bands is None:
        return False
    _, inner, outer = bands
    return is_long_green(c, atr) and outer < c.low <= inner


def _trade(candles: Sequence[Candle], i: int) -> Optional[tuple[float, float, int]]:
    """Enter at the next bar's open, stop at bar i's low, out after HOLD_BARS.

    Returns (R, risk %, exit bar index) or None when the trade cannot exist
    (no next bar, not enough bars to finish, or the open is already through
    the stop). A gap through the stop is booked at the open it gapped to.
    """
    if i + 1 + HOLD_BARS > len(candles):
        return None
    entry = candles[i + 1].open
    sl = candles[i].low
    risk = entry - sl
    if risk <= 0:
        return None
    for j in range(i + 1, i + 1 + HOLD_BARS):
        bar = candles[j]
        if bar.low <= sl:
            exit_px = min(sl, bar.open)
            return (exit_px - entry) / risk, risk / entry * 100, j
    last = i + HOLD_BARS
    return (candles[last].close - entry) / risk, risk / entry * 100, last


def run_rule(candles: Sequence[Candle], rule: str) -> list[dict]:
    """Every trade the rule takes, one position at a time."""
    closes = [c.close for c in candles]
    atrs = _atr_series(candles)
    trades, free_from = [], 0
    for i in range(PERIOD - 1, len(candles)):
        if i < free_from:
            continue
        b = _bands(closes, i)
        hit = (is_naive_signal(candles[i], b) if rule == "naive"
               else is_refined_signal(candles[i], b, atrs[i]))
        if not hit:
            continue
        t = _trade(candles, i)
        if t is None:
            continue
        r, risk_pct, exit_i = t
        trades.append({"r": r, "risk_pct": risk_pct, "time": candles[i + 1].time})
        free_from = exit_i + 1
    return trades


def _net(trades: Sequence[dict], cost_bps: float) -> list[float]:
    return [t["r"] - (cost_bps / 100.0) / t["risk_pct"] for t in trades]


def random_baseline(series: dict[str, Sequence[Candle]], counts: dict[str, int],
                    cost_bps: float, draws: int = RANDOM_DRAWS, seed: int = 11) -> list[float]:
    """Pooled net mean R of random entries, ``draws`` times.

    Same coins, same number of entries per coin as the rule took, same stop
    rule and same exit — and drawn only from bars carrying the same candle
    shape (``is_long_green``), so the one thing left to differ is where the
    candle sat against the bands. That is the Bollinger condition's own
    contribution; what both earn is the market's drift over the window.

    The first version drew from every bar. A random bar's low is often a
    hair under the next open, so its stop is tiny, its R enormous either
    way and its cost ruinous, and the rule — which only takes wide candles —
    "beat" 96% of draws on a pure random walk. Two sides that do not go
    through the same steps are not a pair.
    """
    rng = random.Random(seed)
    # Every eligible bar's outcome is computed once; draws only choose.
    outcomes: dict[str, list[float]] = {}
    for sym, candles in series.items():
        atrs = _atr_series(candles)
        rs = []
        for i in range(PERIOD - 1, len(candles)):
            if not is_long_green(candles[i], atrs[i]):
                continue
            t = _trade(candles, i)
            if t:
                rs.append(t[0] - (cost_bps / 100.0) / t[1])
        outcomes[sym] = rs
    means = []
    for _ in range(draws):
        rs = []
        for sym, n in counts.items():
            pool = outcomes.get(sym) or []
            if pool and n:
                rs.extend(rng.choice(pool) for _ in range(n))
        if rs:
            means.append(statistics.fmean(rs))
    return means


def _summary(rs: Sequence[float]) -> dict:
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs),
        "win_rate": round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1),
        "mean_r": round(statistics.fmean(rs), 3),
        "total_r": round(sum(rs), 1),
    }


# ── report ──────────────────────────────────────────────────────────────────

def backtest(symbols: Sequence[str] = DEFAULT_SYMBOLS, client=None,
             cost_bps: float = 20.0, series: Optional[dict] = None) -> dict:
    """Run both rules over every symbol and set them against random entries."""
    sources: dict[str, str] = {}
    if series is None:
        series = {}
        for sym in symbols:
            candles, source = fetch_history(sym, client)
            sources[sym] = source
            if len(candles) > PERIOD + HOLD_BARS + 2:
                series[sym] = candles
    report: dict = {"cost_bps": cost_bps, "sources": sources, "per_symbol": {}}
    if not series:
        report["error"] = "no candle history could be fetched for any symbol"
        return report

    refined_all, naive_all, counts, years = [], [], {}, {}
    for sym, candles in series.items():
        refined = run_rule(candles, "refined")
        naive = run_rule(candles, "naive")
        counts[sym] = len(refined)
        r_net = _net(refined, cost_bps)
        refined_all += refined
        naive_all += naive
        for t, r in zip(refined, r_net):
            y = time.gmtime(t["time"] / 1000).tm_year
            years.setdefault(y, []).append(r)
        report["per_symbol"][sym] = {
            "bars": len(candles),
            "from": time.strftime("%Y-%m-%d", time.gmtime(candles[0].time / 1000)),
            "refined_net": _summary(r_net),
        }

    refined_net = _net(refined_all, cost_bps)
    report["refined_gross"] = _summary([t["r"] for t in refined_all])
    report["refined_net"] = _summary(refined_net)
    report["naive_net"] = _summary(_net(naive_all, cost_bps))
    report["median_risk_pct"] = (
        round(statistics.median(t["risk_pct"] for t in refined_all), 2) if refined_all else None
    )
    report["years"] = {y: _summary(rs) for y, rs in sorted(years.items())}

    base = random_baseline(series, counts, cost_bps)
    if base and refined_net:
        mean = statistics.fmean(refined_net)
        report["random"] = {
            "median_mean_r": round(statistics.median(base), 3),
            "p95_mean_r": round(sorted(base)[int(0.95 * (len(base) - 1))], 3),
            "share_beaten": round(sum(1 for b in base if b < mean) / len(base) * 100, 1),
            "draws": len(base),
        }
    return report


def render(report: dict) -> str:
    lines = [f"BB-TEST | Bollinger {PERIOD}/{INNER_K}/{OUTER_K} dip rule, {INTERVAL}, "
             f"out after {HOLD_BARS} bars, net of {report['cost_bps']:g}bps"]
    if "error" in report:
        return lines[0] + "\n" + report["error"]
    short = [s for s, src in report["sources"].items() if src.startswith("fallback")]
    missing = [s for s, src in report["sources"].items() if src == "unavailable"]
    if short:
        lines.append(f"WARNING  only ~5.5 months of history for {', '.join(short)} — not the video's 6 years")
    if missing:
        lines.append(f"skipped  no data: {', '.join(missing)}")

    def row(label, s):
        if not s.get("n"):
            return f"{label:<10} no trades"
        return (f"{label:<10} n={s['n']:<4} wr={s['win_rate']:.1f}% "
                f"meanR={s['mean_r']:+.3f} total={s['total_r']:+.1f}R")

    lines += [
        row("naive", report["naive_net"]) + "   (any 2.0-band touch, net)",
        row("refined", report["refined_net"]) + "   (net)",
        row("  gross", report["refined_gross"]),
        f"stop     median 1R = {report['median_risk_pct']}% of entry" if report.get("median_risk_pct") else "",
    ]
    rnd = report.get("random")
    if rnd:
        lines += [
            f"random   same coins, count, stop and exit, random long green candle x{rnd['draws']}:",
            f"         median meanR {rnd['median_mean_r']:+.3f}, p95 {rnd['p95_mean_r']:+.3f} — "
            f"the rule beats {rnd['share_beaten']:.0f}% of random draws",
            "         (95%+ is what 'the rule adds something' needs; below that it is drift)",
        ]
    lines.append("per coin (refined, net)")
    for sym, d in report["per_symbol"].items():
        s = d["refined_net"]
        body = (f"n={s['n']} wr={s['win_rate']:.0f}% meanR={s['mean_r']:+.3f} total={s['total_r']:+.1f}R"
                if s.get("n") else "no trades")
        lines.append(f"  {sym:<9} since {d['from']}  {body}")
    lines.append("per year (refined, net, all coins)")
    for y, s in report["years"].items():
        lines.append(f"  {y}  n={s['n']:<4} meanR={s['mean_r']:+.3f} total={s['total_r']:+.1f}R")
    lines.append("note     rule fixed as stated in the video, not tuned here; trades on "
                 "different coins overlap in time, so n overstates the evidence")
    return "\n".join(l for l in lines if l)
