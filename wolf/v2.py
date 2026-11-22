"""v2 — the trend-breakout rule (``/trendtest`` candidate A) run forward on paper.

It did not pass its offline bar: on six years it beat 95% of its baseline
against a 97.5% line, net +0.080R a trade. The only honest next look is on
bars that did not exist when it was read, so this runs it forward,
**unchanged**, on its own paper ledger, separate from the old bot's. It is
labelled "did not pass" on every message and is never a real-money signal.

The rule is ``wolf.trendtest``'s, called rather than copied (``breakout_side``,
``walk_trail``, ``btc_regime``), so the forward run and the backtest cannot
drift apart. Pre-registered in the registry (``trend-breakout-forward-paper``)
with a kill rule: after ``KILL_AFTER`` closed trades, a net mean below zero
stops new entries for good.

Each pass:
1. fetches the basket's 4h bars, including the bar still forming, whose open
   is the fill for a signal on the bar that just closed;
2. walks each open position over the closed bars since its entry, exactly as
   the backtest would, and closes it on the stop or at ``HOLD_BARS``;
3. checks the newest closed bar of each flat coin for a breakout with BTC's
   regime. Only the newest bar is read: a signal missed while the bot was down
   is skipped, never filled late at a price that no longer exists.
"""

from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

import requests

from wolf.bbtest import DEFAULT_SYMBOLS, _atr_series
from wolf.models import Candle
from wolf.trendtest import (BREAKOUT_BARS, HOLD_BARS, STOP_ATR, TRAIL_MULT, breakout_side,
                            btc_regime, walk_trail)

log = logging.getLogger("wolf.v2")

STATE_KEY = "v2_paper"
BAR_MS = 4 * 3600 * 1000
FETCH_BARS = 500
KILL_AFTER = 300
START_BALANCE = 10_000.0
RISK_PCT = 1.0
LABEL = "🧪 V2 TREND (paper · belum lolos tes)"
_HOSTS = ("https://data-api.binance.vision/api/v3/klines",
          "https://api.binance.com/api/v3/klines")


def fetch_4h(symbol: str, client=None, limit: int = FETCH_BARS, timeout: float = 10.0) -> list[Candle]:
    """The last ``limit`` 4h bars, including the one still forming."""
    for url in _HOSTS:
        try:
            resp = requests.get(url, params={"symbol": symbol, "interval": "4h", "limit": limit},
                                timeout=timeout)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    return [Candle.from_binance(r) for r in rows]
        except (requests.RequestException, ValueError, TypeError, IndexError):
            log.debug("v2 klines via %s failed for %s", url, symbol, exc_info=True)
    if client is not None:
        try:
            return client.get_klines(symbol, "4h", limit) or []
        except Exception:
            log.debug("v2 fallback klines failed for %s", symbol, exc_info=True)
    return []


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _fmt(px: float) -> str:
    return f"{px:,.6g}" if px < 1 else f"{px:,.4f}" if px < 100 else f"{px:,.2f}"


class V2Engine:
    def __init__(self, store, notifier=None, client=None, cost_bps: float = 20.0,
                 thread_id: str = "", symbols: Sequence[str] = DEFAULT_SYMBOLS,
                 fetch: Optional[Callable[[str], list[Candle]]] = None,
                 now_ms: Optional[Callable[[], int]] = None):
        self._store = store
        self._notifier = notifier
        self._cost_bps = cost_bps
        self._thread = thread_id
        self._symbols = tuple(symbols)
        self._fetch = fetch or (lambda s: fetch_4h(s, client))
        self._now = now_ms or (lambda: int(time.time() * 1000))

    # ── state ───────────────────────────────────────────────────────────
    def state(self) -> dict:
        st = self._store.read(STATE_KEY, default=None) or {}
        st.setdefault("started", datetime.now(timezone.utc).isoformat())
        st.setdefault("balance", START_BALANCE)
        st.setdefault("open", {})
        st.setdefault("closed", [])
        st.setdefault("last_bar", {})
        st.setdefault("killed", None)
        return st

    def _save(self, st: dict) -> None:
        self._store.write(STATE_KEY, st)

    def _say(self, text: str) -> None:
        if self._notifier is not None and getattr(self._notifier, "enabled", False):
            self._notifier.send(text, self._thread)

    def net_r(self, r: float, risk_pct: float) -> float:
        return r - (self._cost_bps / 100.0) / risk_pct

    # ── one pass ────────────────────────────────────────────────────────
    def run(self) -> dict:
        st = self.state()
        now = self._now()
        series, closed_n = {}, {}
        for sym in self._symbols:
            bars = self._fetch(sym)
            if bars:
                series[sym] = bars
                closed_n[sym] = sum(1 for b in bars if b.time + BAR_MS <= now)
        btc = series.get("BTCUSDT")
        if not btc:
            log.warning("v2: no BTC bars, pass skipped")
            return {"skipped": "no BTC bars"}
        regime = btc_regime(btc[:closed_n["BTCUSDT"]])
        exits = self._advance(st, series, closed_n)
        entries = [] if st["killed"] else self._enter(st, series, closed_n, regime)
        self._check_kill(st)
        self._save(st)
        return {"exits": exits, "entries": entries}

    def _advance(self, st: dict, series: dict, closed_n: dict) -> list[dict]:
        done = []
        for sym, pos in list(st["open"].items()):
            bars = series.get(sym)
            if not bars:
                continue
            idx = {b.time: n for n, b in enumerate(bars)}
            k = idx.get(pos["entry_time"])
            if k is None:
                continue                    # entry bar fell out of the window; try next pass
            closed = bars[:closed_n[sym]]
            end = min(len(closed), k + HOLD_BARS)
            risk = pos["risk"]
            hit, stop = walk_trail(closed, k, risk, pos["long"], end) if end > k else (None, pos["stop"])
            if hit is None and len(closed) >= k + HOLD_BARS:
                last = closed[k + HOLD_BARS - 1].close
                r = ((last - pos["entry"]) if pos["long"] else (pos["entry"] - last)) / risk
                hit = (r, k + HOLD_BARS - 1, "timeout")
            if hit is None:
                pos["stop"] = stop
                continue
            r, j, how = hit
            net = self.net_r(r, pos["risk_pct"])
            st["balance"] *= 1 + RISK_PCT / 100 * net
            trade = {**pos, "symbol": sym, "r": round(r, 4), "net_r": round(net, 4), "how": how,
                     "exit_time": _iso(closed[j].time)}
            st["closed"].append(trade)
            del st["open"][sym]
            done.append(trade)
            self._say(self._exit_text(trade, st))
        return done

    def _enter(self, st: dict, series: dict, closed_n: dict, regime: dict) -> list[dict]:
        made = []
        for sym, bars in series.items():
            if sym in st["open"]:
                continue
            n = closed_n[sym]
            if n < BREAKOUT_BARS + 15 or n >= len(bars):
                continue                    # no forming bar to fill at, or too little history
            i = n - 1
            if st["last_bar"].get(sym) == bars[i].time:
                continue
            st["last_bar"][sym] = bars[i].time
            long = breakout_side(bars[:n], i, regime)
            if long is None:
                continue
            atr = _atr_series(bars[:n])[i]
            entry_bar = bars[n]
            if atr != atr or atr <= 0 or entry_bar.time != bars[i].time + BAR_MS:
                continue
            entry = entry_bar.open
            risk = STOP_ATR * atr
            if risk >= entry:
                continue
            window = bars[i - BREAKOUT_BARS:i]
            pos = {"long": long, "signal_time": bars[i].time, "entry_time": entry_bar.time,
                   "entry": entry, "risk": risk, "risk_pct": risk / entry * 100,
                   "stop": entry - risk if long else entry + risk,
                   "level": max(b.high for b in window) if long else min(b.low for b in window),
                   "opened": _iso(entry_bar.time)}
            st["open"][sym] = pos
            made.append({"symbol": sym, **pos})
            self._say(self._entry_text(sym, pos))
        return made

    def _check_kill(self, st: dict) -> None:
        if st["killed"] or len(st["closed"]) < KILL_AFTER:
            return
        mean = statistics.fmean(t["net_r"] for t in st["closed"])
        if mean < 0:
            st["killed"] = {"at": datetime.now(timezone.utc).isoformat(), "trades": len(st["closed"]),
                            "mean_net_r": round(mean, 4)}
            self._say(f"{LABEL}\n⛔ Aturan stop kena: setelah {len(st['closed'])} trade rata-rata net "
                      f"{mean:+.3f}R (< 0). v2 berhenti buka posisi baru. Posisi terbuka tetap diselesaikan.")

    # ── messages ────────────────────────────────────────────────────────
    def _entry_text(self, sym: str, pos: dict) -> str:
        side = "LONG 🟢" if pos["long"] else "SHORT 🔴"
        regime = "naik (BTC di atas rata-rata ~50 hari)" if pos["long"] else \
                 "turun (BTC di bawah rata-rata ~50 hari)"
        why = (f"candle 4h tutup di atas high {BREAKOUT_BARS} candle ({_fmt(pos['level'])})"
               if pos["long"] else
               f"candle 4h tutup di bawah low {BREAKOUT_BARS} candle ({_fmt(pos['level'])})")
        return (f"{LABEL}\n{side} {sym}\n"
                f"Regime BTC: {regime}\nAlasan: {why}\n"
                f"Entry (open 4h): {_fmt(pos['entry'])}\n"
                f"Stop awal: {_fmt(pos['stop'])} ({STOP_ATR:g}×ATR = {pos['risk_pct']:.2f}%)\n"
                f"Exit: trailing {STOP_ATR * TRAIL_MULT:g}×ATR dari harga terbaik, tanpa TP, maks 15 hari\n"
                f"Paper only — aturan ini gagal tipis di backtest (95% vs syarat 97.5%). Bukan sinyal uang beneran.")

    def _exit_text(self, t: dict, st: dict) -> str:
        how = {"stop": "kena stop/trailing", "timeout": "batas 15 hari"}.get(t["how"], t["how"])
        side = "LONG" if t["long"] else "SHORT"
        return (f"{LABEL}\n✅ Tutup {side} {t['symbol']} — {how}\n"
                f"Hasil: {t['r']:+.2f}R kotor, {t['net_r']:+.2f}R setelah biaya\n"
                f"Trade ke-{len(st['closed'])} · saldo paper {st['balance']:,.2f}")

    # ── dashboard ───────────────────────────────────────────────────────
    def summary(self, recent: int = 30) -> dict:
        """Everything the Vercel "Strategi v2" tab shows, from the same ledger
        the card reads. The curve compounds each closed trade at RISK_PCT, net
        and gross, so the two lines differ by cost alone."""
        st = self.state()
        closed = st["closed"]
        net_bal = gross_bal = 1.0
        curve = []
        for t in closed:
            net_bal *= 1 + RISK_PCT / 100 * t["net_r"]
            gross_bal *= 1 + RISK_PCT / 100 * t["r"]
            ts = int(datetime.fromisoformat(t["exit_time"]).timestamp())
            curve.append({"t": ts, "net": round((net_bal - 1) * 100, 3),
                          "gross": round((gross_bal - 1) * 100, 3)})
        nets = [t["net_r"] for t in closed]

        def side(flag):
            rs = [t["net_r"] for t in closed if t["long"] is flag]
            return {"n": len(rs), "net_r": round(statistics.fmean(rs), 4) if rs else None}

        return {
            "label": LABEL, "started": st["started"], "start_balance": START_BALANCE,
            "balance": round(st["balance"], 2),
            "return_pct": round((st["balance"] / START_BALANCE - 1) * 100, 3),
            "risk_pct": RISK_PCT, "cost_bps": self._cost_bps,
            "n": len(closed), "wins": sum(1 for x in nets if x > 0),
            "net_r": round(statistics.fmean(nets), 4) if nets else None,
            "gross_r": round(statistics.fmean(t["r"] for t in closed), 4) if closed else None,
            "long": side(True), "short": side(False),
            "kill_after": KILL_AFTER, "killed": st["killed"],
            "rules": {"breakout_bars": BREAKOUT_BARS, "stop_atr": STOP_ATR,
                      "trail_atr": STOP_ATR * TRAIL_MULT, "hold_days": HOLD_BARS // 6},
            "open": [{"symbol": sym, "direction": "LONG" if p["long"] else "SHORT",
                      "entry": p["entry"], "stop": p["stop"], "initial_stop":
                      p["entry"] - p["risk"] if p["long"] else p["entry"] + p["risk"],
                      "risk_pct": round(p["risk_pct"], 3), "level": p.get("level"),
                      "opened": p["opened"], "signal_time": _iso(p["signal_time"])}
                     for sym, p in sorted(st["open"].items())],
            "closed": [{"symbol": t["symbol"], "direction": "LONG" if t["long"] else "SHORT",
                        "entry": t["entry"], "r": t["r"], "net_r": t["net_r"], "how": t["how"],
                        "opened": t["opened"], "exit_time": t["exit_time"]}
                       for t in reversed(closed[-recent:])],
            "curve": curve,
        }

    # ── card ────────────────────────────────────────────────────────────
    def card(self) -> str:
        st = self.state()
        lines = [f"{LABEL}", f"mulai    {st['started'][:16].replace('T', ' ')} UTC · "
                 f"risk {RISK_PCT:g}%/trade · biaya {self._cost_bps:g}bps"]
        closed = st["closed"]
        lines.append(f"saldo    {st['balance']:,.2f} (awal {START_BALANCE:,.0f}, "
                     f"{(st['balance'] / START_BALANCE - 1) * 100:+.2f}%)")
        if closed:
            nets = [t["net_r"] for t in closed]
            wins = sum(1 for x in nets if x > 0)
            sd = statistics.stdev(nets) if len(nets) > 1 else 0.0
            lines.append(f"selesai  {len(closed)} trade · menang {wins} ({wins / len(nets) * 100:.0f}%) · "
                         f"rata-rata {statistics.fmean(nets):+.3f}R net (sd {sd:.2f}) · total {sum(nets):+.1f}R")
            for side, flag in (("LONG", True), ("SHORT", False)):
                rs = [t["net_r"] for t in closed if t["long"] is flag]
                if rs:
                    lines.append(f"  {side:<5} n={len(rs)} rata-rata {statistics.fmean(rs):+.3f}R")
        else:
            lines.append("selesai  belum ada trade yang tutup")
        if st["open"]:
            lines.append(f"terbuka  {len(st['open'])} posisi")
            for sym, p in sorted(st["open"].items()):
                lines.append(f"  {'LONG ' if p['long'] else 'SHORT'} {sym:<9} entry {_fmt(p['entry'])} "
                             f"stop {_fmt(p['stop'])} sejak {p['opened'][:16].replace('T', ' ')}")
        else:
            lines.append("terbuka  tidak ada posisi")
        if st["killed"]:
            k = st["killed"]
            lines.append(f"STATUS   DIHENTIKAN setelah {k['trades']} trade (rata-rata {k['mean_net_r']:+.3f}R)")
        else:
            lines.append(f"aturan   cek mati di {KILL_AFTER} trade: rata-rata net < 0 → berhenti "
                         f"({len(closed)}/{KILL_AFTER})")
        lines.append("catatan  backtest-nya gagal tipis; ini cek di data baru, bukan bukti. "
                     f"~1–2 trade/hari: {KILL_AFTER} trade cuma bisa nangkep kalau rusak, belum membuktikan bagus.")
        return "\n".join(lines)
