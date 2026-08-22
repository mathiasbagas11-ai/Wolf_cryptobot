"""Whale tracker → 👁 Whale Report topic.

Two complementary views of "smart money":

* **Positioning** — the futures global long/short *account* ratio plus
  open-interest drift per symbol, i.e. whether the crowd is net long or short
  and whether positions are being *accumulated* (OI rising) or unwound. This is
  what answers "are whales long/short/accumulating this coin".
* **Large trades** — recent public spot trades whose notional clears a USD
  threshold. Seen trade IDs are remembered in the state store so the same whale
  isn't alerted twice.

Everything uses public REST data (no API key, no WebSocket); any piece that a
venue can't serve is simply omitted.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf.state import StateStore
from wolf.textfmt import DIVIDER, esc, fmt_price, fmt_usd, now

log = logging.getLogger("wolf.reports")

SEEN_KEY = "whale_seen"
SEEN_CAP = 1000
DEFAULT_WHALE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


class WhaleTracker:
    def __init__(self, client, store: StateStore, symbols: Sequence[str] = DEFAULT_WHALE_SYMBOLS,
                 min_usd: float = 250_000, max_alerts: int = 5, tz: str = "UTC") -> None:
        self._client = client
        self._store = store
        self._symbols = list(symbols)
        self._min_usd = min_usd
        self._max_alerts = max_alerts
        self._tz = tz

    @staticmethod
    def _base(symbol: str) -> str:
        return symbol[:-4] if symbol.endswith("USDT") else symbol

    def _positioning_lines(self) -> list[str]:
        """Per-symbol long/short bias + accumulation, when futures data exists."""
        get_ls = getattr(self._client, "get_long_short_ratio", None)
        get_oi = getattr(self._client, "get_open_interest_change", None)
        if not callable(get_ls):
            return []
        rows = []
        for sym in self._symbols:
            ls = get_ls(sym)
            if not ls:
                continue
            oi = get_oi(sym) if callable(get_oi) else None
            ratio = ls["ratio"]
            if ratio >= 1.15:
                bias, emoji = "longs dominate", "🟢"
            elif ratio <= 0.87:
                bias, emoji = "shorts dominate", "🔴"
            else:
                bias, emoji = "balanced", "⚪"
            row = (
                f"{emoji} <b>{esc(self._base(sym))}</b>  L/S {ratio:.2f} · {bias} "
                f"({ls['long_pct']:.0f}%L/{ls['short_pct']:.0f}%S)"
            )
            if oi is not None:
                if oi >= 2.0:
                    row += f" · OI {oi:+.1f}% 📈 accumulating"
                elif oi <= -2.0:
                    row += f" · OI {oi:+.1f}% 📉 unwinding"
                else:
                    row += f" · OI {oi:+.1f}%"
            rows.append(row)
        return rows

    def _trade_lines(self) -> list[str]:
        seen_list = self._store.read(SEEN_KEY, default=[])
        seen = set(seen_list)
        found = []
        for sym in self._symbols:
            for t in self._client.get_recent_trades(sym, limit=500):
                if t["usd"] >= self._min_usd and t["id"] not in seen:
                    found.append(t)
        if not found:
            return []
        # Suppress the whole found batch so nothing is re-alerted next cycle.
        seen_list = (seen_list + [t["id"] for t in found])[-SEEN_CAP:]
        self._store.write(SEEN_KEY, seen_list)

        found.sort(key=lambda t: t["usd"], reverse=True)
        rows = []
        for t in found[: self._max_alerts]:
            emoji = "🟢" if t["side"] == "BUY" else "🔴"
            rows.append(
                f"{emoji} <b>{esc(self._base(t['symbol']))}</b> {t['side']}  "
                f"{fmt_usd(t['usd'])} @ <code>{fmt_price(t['price'])}</code>"
            )
        return rows

    def build(self) -> Optional[str]:
        positioning = self._positioning_lines()
        trades = self._trade_lines()
        if not positioning and not trades:
            return None

        lines = [f"👁 <b>WHALE REPORT</b>\n{DIVIDER}"]
        if positioning:
            lines.append("<b>Positioning (futures)</b>")
            lines.extend(positioning)
        if trades:
            if positioning:
                lines.append("")
            lines.append("<b>Large trades</b>")
            lines.extend(trades)
        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
