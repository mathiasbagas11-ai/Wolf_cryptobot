"""Majors session report → 🐝 BTC/ETH/SOL topic.

A periodic snapshot of the major coins (price + 24h change), built from the
exchange's one-request 24h overview so it costs a single API call regardless of
how many majors are listed.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from wolf.textfmt import DIVIDER, esc, fmt_price, now

log = logging.getLogger("wolf.reports")

DEFAULT_MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


class MajorsReporter:
    def __init__(self, client, symbols: Sequence[str] = DEFAULT_MAJORS, tz: str = "UTC") -> None:
        self._client = client
        self._symbols = list(symbols)
        self._tz = tz

    def build(self) -> Optional[str]:
        overview = {o["symbol"]: o for o in self._client.get_market_overview()}
        if not overview:
            return None
        lines = [f"🐝 <b>MAJORS — SESSION REPORT</b>\n{DIVIDER}"]
        found = False
        for sym in self._symbols:
            o = overview.get(sym)
            if not o:
                continue
            found = True
            emoji = "🟢" if o["change_pct"] >= 0 else "🔴"
            base = sym[:-4] if sym.endswith("USDT") else sym
            lines.append(
                f"{emoji} <b>{esc(base)}</b>  <code>{fmt_price(o['price'])}</code>  "
                f"({o['change_pct']:+.2f}%)"
            )
        if not found:
            return None
        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
