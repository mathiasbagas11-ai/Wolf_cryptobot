"""OpenBot challenger — an LLM agent's calls, graded by Wolf's own referee.

The owner runs an agent in the OpenBot desktop app (a Gemini model) that
decides trades by itself, and asked to see how it does against the bot
("kita lihat seberapa gacor"). The agent is outside this codebase; this
module is only the referee, so the two are scored the same way:

* the agent names a coin, a side, a stop, a target and a time limit;
* **Wolf sets the entry**, at the market price when the call arrives, so the
  agent cannot quote itself a fill that never existed;
* grading walks 15m bars that open after the entry: stop or target, the stop
  counted first on a bar that touches both, otherwise the close at the time
  limit;
* every trade pays ``cost_bps`` on its own stop, as v2 and ``/montecarlo`` do.

It keeps its own ledger (``openbot_paper``) and never touches the tracker, so
the old bot's sample and v2's run are unchanged. Nothing here is on the
signal path: Wolf records the agent's calls, it does not act on them (rule 4).

Pre-registered in the registry as ``openbot-llm-challenger``.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

import requests

from wolf.bbtest import DEFAULT_SYMBOLS
from wolf.models import Candle

log = logging.getLogger("wolf.openbot")

STATE_KEY = "openbot_paper"
BAR_MS = 15 * 60 * 1000
START_BALANCE = 10_000.0
RISK_PCT = 1.0
MAX_OPEN = 5
MIN_STOP_PCT = 0.2
MAX_STOP_PCT = 15.0
MIN_HOURS, MAX_HOURS, DEFAULT_HOURS = 1.0, 72.0, 24.0
READ_AT = 100
LABEL = "🤖 OPENBOT (agent Gemini · paper)"
_HOSTS = ("https://data-api.binance.vision/api/v3/klines",
          "https://api.binance.com/api/v3/klines")


def fetch_15m(symbol: str, client=None, limit: int = 400, timeout: float = 10.0) -> list[Candle]:
    """The last ``limit`` 15m bars, including the one still forming."""
    for url in _HOSTS:
        try:
            resp = requests.get(url, params={"symbol": symbol, "interval": "15m", "limit": limit},
                                timeout=timeout)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    return [Candle.from_binance(r) for r in rows]
        except (requests.RequestException, ValueError, TypeError, IndexError):
            log.debug("openbot klines via %s failed for %s", url, symbol, exc_info=True)
    if client is not None:
        try:
            return client.get_klines(symbol, "15m", limit) or []
        except Exception:
            log.debug("openbot fallback klines failed for %s", symbol, exc_info=True)
    return []


def normalise_symbol(raw: str) -> str:
    s = (raw or "").strip().upper().replace("/", "").replace("-", "")
    return s if s.endswith("USDT") else s + "USDT"


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _fmt(px: float) -> str:
    return f"{px:,.6g}" if px < 1 else f"{px:,.4f}" if px < 100 else f"{px:,.2f}"


def grade(bars: Sequence[Candle], pos: dict, now_ms: int) -> Optional[tuple[float, float, int, str]]:
    """``(r, exit_price, exit_ms, how)`` once the position is decided, else None.

    Only bars that open at or after ``pos['grade_from']`` and have closed by
    ``now_ms`` are read. A bar that reaches both the stop and the target is
    booked as the stop: the bar's path is unknown, and this charges the
    uncertainty to the agent rather than flattering it.
    """
    long, entry, risk = pos["long"], pos["entry"], pos["risk"]
    deadline = pos["deadline_ms"]
    last = None
    for b in bars:
        if b.time < pos["grade_from"] or b.time + BAR_MS > now_ms:
            continue
        if b.time + BAR_MS > deadline:
            break
        last = b
        hit_sl = b.low <= pos["sl"] if long else b.high >= pos["sl"]
        hit_tp = b.high >= pos["tp"] if long else b.low <= pos["tp"]
        if hit_sl:
            return -1.0, pos["sl"], b.time + BAR_MS, "stop"
        if hit_tp:
            r = (pos["tp"] - entry) / risk if long else (entry - pos["tp"]) / risk
            return r, pos["tp"], b.time + BAR_MS, "target"
    if now_ms >= deadline and last is not None:
        r = (last.close - entry) / risk if long else (entry - last.close) / risk
        return r, last.close, last.time + BAR_MS, "timeout"
    return None


class OpenBotLedger:
    def __init__(self, store, notifier=None, client=None, cost_bps: float = 20.0,
                 thread_id: str = "", symbols: Sequence[str] = DEFAULT_SYMBOLS,
                 fetch: Optional[Callable[[str], list[Candle]]] = None,
                 now_ms: Optional[Callable[[], int]] = None,
                 tracker=None, v2=None):
        self._store = store
        self._notifier = notifier
        self._cost_bps = cost_bps
        self._thread = thread_id
        self._symbols = tuple(symbols)
        self._fetch = fetch or (lambda s: fetch_15m(s, client))
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._tracker = tracker
        self._v2 = v2

    # ── state ───────────────────────────────────────────────────────────
    def state(self) -> dict:
        st = self._store.read(STATE_KEY, default=None) or {}
        st.setdefault("started", None)          # set by the first accepted call
        st.setdefault("balance", START_BALANCE)
        st.setdefault("open", {})
        st.setdefault("closed", [])
        st.setdefault("received", 0)
        st.setdefault("refused", {})
        return st

    def _save(self, st: dict) -> None:
        self._store.write(STATE_KEY, st)

    def _say(self, text: str) -> None:
        if self._notifier is not None and getattr(self._notifier, "enabled", False):
            self._notifier.send(text, self._thread)

    def net_r(self, r: float, risk_pct: float) -> float:
        return r - (self._cost_bps / 100.0) / risk_pct

    # ── a call from the agent ───────────────────────────────────────────
    def submit(self, symbol: str, direction: str, sl: float, tp: float,
               hours: Optional[float] = None, reason: str = "", source: str = "api") -> dict:
        """Record one call. Refusals are counted, never silently dropped."""
        st = self.state()
        st["received"] += 1
        out = self._check(st, symbol, direction, sl, tp, hours)
        if "refused" in out:
            st["refused"][out["refused"]] = st["refused"].get(out["refused"], 0) + 1
            self._save(st)
            return out
        sym, long, entry, bar_time, hours, sl, tp = (out["symbol"], out["long"], out["entry"],
                                                     out["bar_time"], out["hours"], out["sl"], out["tp"])
        now = self._now()
        risk = abs(entry - sl)
        pos = {"long": long, "entry": entry, "sl": float(sl), "tp": float(tp),
               "risk": risk, "risk_pct": risk / entry * 100,
               "rr": abs(tp - entry) / risk, "hours": hours,
               "opened_ms": now, "opened": _iso(now),
               "grade_from": bar_time + BAR_MS,
               "deadline_ms": int(now + hours * 3_600_000),
               "reason": (reason or "")[:500], "source": source}
        st["started"] = st["started"] or _iso(now)
        st["open"][sym] = pos
        self._save(st)
        self._say(self._entry_text(sym, pos))
        return {"recorded": True, "symbol": sym, **pos}

    def _check(self, st, symbol, direction, sl, tp, hours) -> dict:
        sym = normalise_symbol(symbol)
        if sym not in self._symbols:
            return {"refused": "coin_outside_universe",
                    "detail": f"{sym} bukan salah satu dari {len(self._symbols)} koin: "
                              + ", ".join(s[:-4] for s in self._symbols)}
        d = (direction or "").strip().upper()
        if d not in ("LONG", "SHORT"):
            return {"refused": "bad_direction", "detail": "arah harus LONG atau SHORT"}
        long = d == "LONG"
        if sym in st["open"]:
            return {"refused": "already_open", "detail": f"{sym} masih punya posisi terbuka"}
        if len(st["open"]) >= MAX_OPEN:
            return {"refused": "too_many_open", "detail": f"maksimal {MAX_OPEN} posisi terbuka"}
        try:
            sl, tp = float(sl), float(tp)
            hours = DEFAULT_HOURS if hours in (None, "") else float(hours)
        except (TypeError, ValueError):
            return {"refused": "bad_number", "detail": "SL, TP dan jam harus angka"}
        if not (MIN_HOURS <= hours <= MAX_HOURS):
            return {"refused": "bad_hours", "detail": f"batas waktu {MIN_HOURS:g}–{MAX_HOURS:g} jam"}
        bars = self._fetch(sym)
        now = self._now()
        forming = [b for b in bars if b.time <= now < b.time + BAR_MS]
        if not forming:
            return {"refused": "no_price", "detail": "harga live tidak tersedia, coba lagi"}
        entry = forming[-1].close
        if long and not (sl < entry < tp) or not long and not (tp < entry < sl):
            return {"refused": "levels_wrong_side",
                    "detail": f"harga sekarang {_fmt(entry)}: LONG butuh SL < harga < TP, "
                              "SHORT butuh TP < harga < SL"}
        stop_pct = abs(entry - sl) / entry * 100
        if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
            return {"refused": "stop_width",
                    "detail": f"jarak SL {stop_pct:.2f}% (boleh {MIN_STOP_PCT:g}–{MAX_STOP_PCT:g}%)"}
        return {"symbol": sym, "long": long, "entry": entry, "bar_time": forming[-1].time,
                "hours": hours, "sl": sl, "tp": tp}

    # ── the grading pass ────────────────────────────────────────────────
    def run(self) -> dict:
        st = self.state()
        if not st["open"]:
            return {"exits": []}
        now = self._now()
        done = []
        for sym, pos in list(st["open"].items()):
            bars = self._fetch(sym)
            if not bars:
                continue
            hit = grade(bars, pos, now)
            if hit is None:
                continue
            r, px, ms, how = hit
            net = self.net_r(r, pos["risk_pct"])
            st["balance"] *= 1 + RISK_PCT / 100 * net
            trade = {**pos, "symbol": sym, "r": round(r, 4), "net_r": round(net, 4),
                     "exit_price": px, "how": how, "exit_time": _iso(ms)}
            st["closed"].append(trade)
            del st["open"][sym]
            done.append(trade)
            self._say(self._exit_text(trade, st))
        self._save(st)
        return {"exits": done}

    # ── messages ────────────────────────────────────────────────────────
    def _entry_text(self, sym: str, p: dict) -> str:
        side = "LONG 🟢" if p["long"] else "SHORT 🔴"
        why = f"\nAlasan agent: {p['reason']}" if p.get("reason") else ""
        return (f"{LABEL}\n{side} {sym}\n"
                f"Entry (harga pasar saat diterima): {_fmt(p['entry'])}\n"
                f"SL {_fmt(p['sl'])} ({p['risk_pct']:.2f}%) · TP {_fmt(p['tp'])} ({p['rr']:.2f}R) · "
                f"maks {p['hours']:g} jam{why}\n"
                "Keputusan LLM, dinilai wasit Wolf. Paper only.")

    def _exit_text(self, t: dict, st: dict) -> str:
        how = {"stop": "kena SL", "target": "kena TP", "timeout": "waktu habis"}[t["how"]]
        side = "LONG" if t["long"] else "SHORT"
        return (f"{LABEL}\n✅ Tutup {side} {t['symbol']} — {how}\n"
                f"Hasil: {t['r']:+.2f}R kotor, {t['net_r']:+.2f}R setelah biaya\n"
                f"Trade ke-{len(st['closed'])} · saldo paper {st['balance']:,.2f}")

    # ── comparison ──────────────────────────────────────────────────────
    @staticmethod
    def _summ(nets: list[float], grosses: list[float], days: list[str]) -> dict:
        if not nets:
            return {"n": 0}
        by_day = defaultdict(list)
        for d, x in zip(days, nets):
            by_day[d].append(x)
        dm = [statistics.fmean(v) for v in by_day.values()]
        t = None
        if len(dm) > 1 and statistics.stdev(dm) > 0:
            t = statistics.fmean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
        return {"n": len(nets), "wins": sum(1 for x in nets if x > 0),
                "gross": statistics.fmean(grosses), "net": statistics.fmean(nets),
                "days": len(dm), "t_day": t}

    def compare(self) -> dict:
        """OpenBot, v2 and the old bot over the same window: trades that closed
        since OpenBot's first call. A level comparison, not paired — the three
        trade different moments — so read it for size, not as a contest."""
        st = self.state()
        out = {"started": st["started"]}
        c = st["closed"]
        out["openbot"] = self._summ([t["net_r"] for t in c], [t["r"] for t in c],
                                    [t["exit_time"][:10] for t in c])
        if not st["started"]:
            return out
        start = datetime.fromisoformat(st["started"])
        if self._v2 is not None:
            try:
                vc = [t for t in self._v2.state()["closed"]
                      if datetime.fromisoformat(t["exit_time"]) >= start]
                out["v2"] = self._summ([t["net_r"] for t in vc], [t["r"] for t in vc],
                                       [t["exit_time"][:10] for t in vc])
            except Exception:
                log.debug("openbot: v2 comparison failed", exc_info=True)
        if self._tracker is not None:
            try:
                from wolf.montecarlo import _trade_rows
                rows = [r for r in _trade_rows(self._tracker.history(), self._cost_bps, None)
                        if r["at"] >= start]
                out["old"] = self._summ([r["net"] for r in rows], [r["gross"] for r in rows],
                                        [r["at"].date().isoformat() for r in rows])
            except Exception:
                log.debug("openbot: old-bot comparison failed", exc_info=True)
        return out

    # ── card ────────────────────────────────────────────────────────────
    def card(self) -> str:
        st = self.state()
        cmp_ = self.compare()
        lines = [LABEL]
        if not st["started"]:
            lines += ["belum ada call dari agent.",
                      "kirim: /ob BTC LONG <SL> <TP> [jam] [alasan]",
                      f"aturan: {len(self._symbols)} koin, entry = harga pasar saat diterima, "
                      f"SL {MIN_STOP_PCT:g}–{MAX_STOP_PCT:g}%, {MIN_HOURS:g}–{MAX_HOURS:g} jam, "
                      f"maks {MAX_OPEN} posisi, biaya {self._cost_bps:g}bps"]
            return "\n".join(lines)
        lines.append(f"mulai    {st['started'][:16].replace('T', ' ')} UTC · risk {RISK_PCT:g}%/trade · "
                     f"biaya {self._cost_bps:g}bps")
        lines.append(f"saldo    {st['balance']:,.2f} (awal {START_BALANCE:,.0f}, "
                     f"{(st['balance'] / START_BALANCE - 1) * 100:+.2f}%)")
        refused = sum(st["refused"].values())
        lines.append(f"call     {st['received']} diterima · {refused} ditolak"
                     + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(st["refused"].items())) + ")"
                        if refused else ""))
        c = st["closed"]
        if c:
            hows = defaultdict(int)
            for t in c:
                hows[t["how"]] += 1
            lines.append(f"tutup    TP {hows['target']} · SL {hows['stop']} · waktu habis {hows['timeout']}")
        if st["open"]:
            lines.append(f"terbuka  {len(st['open'])} posisi")
            for sym, p in sorted(st["open"].items()):
                lines.append(f"  {'LONG ' if p['long'] else 'SHORT'} {sym:<9} entry {_fmt(p['entry'])} "
                             f"SL {_fmt(p['sl'])} TP {_fmt(p['tp'])} s/d "
                             f"{_iso(p['deadline_ms'])[5:16].replace('T', ' ')}")
        lines.append("")
        lines.append("perbandingan (trade yang tutup sejak call pertama):")
        lines.append(f"  {'':<8}{'n':>4} {'menang':>7} {'kotor':>7} {'net':>7} {'hari':>5} {'t(hari)':>8}")
        for key, name in (("openbot", "OpenBot"), ("v2", "v2"), ("old", "bot lama")):
            s = cmp_.get(key)
            if s is None:
                continue
            if not s["n"]:
                lines.append(f"  {name:<8}{0:>4}  belum ada")
                continue
            t = f"{s['t_day']:+.2f}" if s["t_day"] is not None else "—"
            lines.append(f"  {name:<8}{s['n']:>4} {s['wins'] / s['n'] * 100:>6.0f}% "
                         f"{s['gross']:>+7.3f} {s['net']:>+7.3f} {s['days']:>5} {t:>8}")
        n = len(c)
        lines.append("")
        lines.append(f"aturan   dibaca di {READ_AT} trade ({n}/{READ_AT}). 'Gacor' cuma kalau net > 0 "
                     "DAN t(hari) ≥ 2. Selain itu: belum terbukti.")
        lines.append("catatan  kirim SEMUA call agent, termasuk yang lo ragu — kalau dipilih-pilih, "
                     "yang diukur jadi selera lo, bukan agent-nya.")
        return "\n".join(lines)


def parse_command(arg: str) -> dict:
    """``BTC LONG 58000 62000 [jam] [alasan ...]`` → submit() kwargs."""
    parts = (arg or "").split()
    if len(parts) < 4:
        raise ValueError("format: /ob BTC LONG <SL> <TP> [jam] [alasan]")
    sym, d, sl, tp = parts[:4]
    hours, rest = None, parts[4:]
    if rest:
        try:
            hours = float(rest[0].lower().rstrip("hj"))
            rest = rest[1:]
        except ValueError:
            pass
    return {"symbol": sym, "direction": d, "sl": sl.replace(",", ""),
            "tp": tp.replace(",", ""), "hours": hours, "reason": " ".join(rest)}
