"""Whale tracker → 👁 Whale Report topic.

Polls recent public trades for a small set of symbols and flags ones whose
notional (price × qty) clears a USD threshold. Seen trade IDs are remembered in
the state store so the same whale isn't alerted twice. Uses only public REST
trade data (no API key, no WebSocket); if the venue is unavailable it simply
produces nothing.
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

    def build(self) -> Optional[str]:
        seen_list = self._store.read(SEEN_KEY, default=[])
        seen = set(seen_list)
        found = []
        for sym in self._symbols:
            for t in self._client.get_recent_trades(sym, limit=500):
                if t["usd"] >= self._min_usd and t["id"] not in seen:
                    found.append(t)
        if not found:
            return None
        # Alert the biggest trades first; only mark the ones we actually report
        # as seen, so any overflow beyond max_alerts surfaces on a later cycle
        # (while it is still in the recent-trades window) instead of being lost.
        found.sort(key=lambda t: t["usd"], reverse=True)
        to_alert = found[: self._max_alerts]
        seen_list = (seen_list + [t["id"] for t in to_alert])[-SEEN_CAP:]
        self._store.write(SEEN_KEY, seen_list)

        lines = [f"👁 <b>WHALE REPORT</b>\n{DIVIDER}"]
        for t in to_alert:
            base = t["symbol"][:-4] if t["symbol"].endswith("USDT") else t["symbol"]
            emoji = "🟢" if t["side"] == "BUY" else "🔴"
            lines.append(
                f"{emoji} <b>{esc(base)}</b> {t['side']}  {fmt_usd(t['usd'])} "
                f"@ <code>{fmt_price(t['price'])}</code>"
            )
        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
