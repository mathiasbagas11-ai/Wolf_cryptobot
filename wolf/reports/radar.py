"""Market radar → 🔥 Hot Ecosystem topic.

Scans the whole market via the exchange's one-request 24h overview (so it's a
single API call, not per-symbol) and surfaces the top gainers, losers and
volume leaders among liquid USDT pairs.
"""

from __future__ import annotations

import logging
from typing import Optional

from wolf.textfmt import DIVIDER, esc, fmt_price, fmt_usd, now

log = logging.getLogger("wolf.reports")


class MarketRadar:
    def __init__(self, client, top_n: int = 3, min_quote_volume: float = 5_000_000,
                 quote: str = "USDT", tz: str = "UTC") -> None:
        self._client = client
        self._top_n = top_n
        self._min_vol = min_quote_volume
        self._quote = quote
        self._tz = tz

    def build(self) -> Optional[str]:
        rows = [
            o for o in self._client.get_market_overview()
            if o["symbol"].endswith(self._quote) and o["quote_volume"] >= self._min_vol
        ]
        if not rows:
            return None
        gainers = sorted(rows, key=lambda r: r["change_pct"], reverse=True)[: self._top_n]
        losers = sorted(rows, key=lambda r: r["change_pct"])[: self._top_n]
        movers = sorted(rows, key=lambda r: r["quote_volume"], reverse=True)[: self._top_n]

        def _line(o: dict) -> str:
            base = o["symbol"][: -len(self._quote)]
            return (f"  {esc(base)}  {o['change_pct']:+.2f}%  "
                    f"<code>{fmt_price(o['price'])}</code>  ({fmt_usd(o['quote_volume'])})")

        lines = [f"🔥 <b>MARKET RADAR</b>\n{DIVIDER}", "📈 <b>Top gainers</b>"]
        lines += [_line(o) for o in gainers]
        lines.append("📉 <b>Top losers</b>")
        lines += [_line(o) for o in losers]
        lines.append("🔊 <b>Volume leaders</b>")
        lines += [_line(o) for o in movers]
        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
