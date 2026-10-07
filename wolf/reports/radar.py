"""Market radar → 🔥 Hot Ecosystem topic.

Scans the whole market via the exchange's one-request 24h overview (so it's a
single API call, not per-symbol) and surfaces the top gainers, losers and — when
the venue serves order-book depth — the fastest **turnover** relative to
liquidity.

Turnover is the more useful third board. A raw volume ranking returns the same
handful of mega-caps every cycle, whereas volume measured against the size
actually resting in the book says a market is churning through its own
liquidity, which is where short-horizon moves come from. It is a statement
about activity, not about safety.
"""

from __future__ import annotations

import logging
from typing import Optional

from wolf.orderflow import turnover
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
        depth = self._depth()
        for row in rows:
            row["vl"] = turnover(row["quote_volume"], depth.get(row["symbol"], 0.0))

        gainers = sorted(rows, key=lambda r: r["change_pct"], reverse=True)[: self._top_n]
        losers = sorted(rows, key=lambda r: r["change_pct"])[: self._top_n]
        churn = sorted(
            (r for r in rows if r["vl"] == r["vl"]),   # drop NaN
            key=lambda r: r["vl"], reverse=True,
        )

        def _line(o: dict) -> str:
            base = o["symbol"][: -len(self._quote)]
            vl = o.get("vl", float("nan"))
            vl_txt = f"  V/L {vl:,.0f}" if vl == vl else ""
            return (f"  {esc(base)}  {o['change_pct']:+.2f}%  "
                    f"<code>{fmt_price(o['price'])}</code>  "
                    f"({fmt_usd(o['quote_volume'])}){vl_txt}")

        lines = [f"🔥 <b>MARKET RADAR</b>\n{DIVIDER}", "📈 <b>Top gainers</b>"]
        lines += [_line(o) for o in gainers]
        lines.append("📉 <b>Top losers</b>")
        lines += [_line(o) for o in losers]
        if churn:
            lines.append("🌀 <b>Fastest turnover (V/L)</b>")
            lines += [_line(o) for o in churn[: self._top_n]]
            lines.append(
                "<i>V/L = 24h volume ÷ top-of-book depth. High means fast churn "
                "against thin resting size — activity, not safety.</i>"
            )
        else:
            # No depth from this venue: rank by raw volume rather than print a
            # ratio computed against a denominator we do not have.
            movers = sorted(rows, key=lambda r: r["quote_volume"], reverse=True)[: self._top_n]
            lines.append("🔊 <b>Volume leaders</b>")
            lines += [_line(o) for o in movers]
        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)

    def _depth(self) -> dict[str, float]:
        """Book depth per symbol, or ``{}`` when the venue cannot serve it."""
        try:
            return self._client.get_book_depth()
        except (AttributeError, TypeError):
            return {}   # older client or a test double without the capability
