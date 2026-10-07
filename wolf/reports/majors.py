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

_MAJORS_SYSTEM = (
    "You are a crypto desk strategist writing a post-session wrap for BTC/ETH/SOL "
    "and majors. Given each coin's price and 24h change, write a 1-2 sentence "
    "outlook for the next session: who led/lagged, risk-on vs risk-off tone, and "
    "what to watch. Concrete and concise. No preamble, no disclaimers."
)


class MajorsReporter:
    def __init__(self, client, symbols: Sequence[str] = DEFAULT_MAJORS, tz: str = "UTC", llm=None) -> None:
        self._client = client
        self._symbols = list(symbols)
        self._tz = tz
        self._llm = llm

    def _narrative(self, rows: list[dict]) -> str:
        if self._llm is None or not getattr(self._llm, "available", False):
            return ""
        facts = "\n".join(
            f"{r['base']}: {r['price']:.4g} ({r['change_pct']:+.2f}% 24h)" for r in rows
        )
        try:
            return self._llm.complete(_MAJORS_SYSTEM, facts, max_tokens=200).strip()
        except Exception:
            log.warning("Majors narrative failed", exc_info=True)
            return ""

    def build(self) -> Optional[str]:
        overview = {o["symbol"]: o for o in self._client.get_market_overview()}
        if not overview:
            return None
        rows = []
        for sym in self._symbols:
            o = overview.get(sym)
            if not o:
                continue
            base = sym[:-4] if sym.endswith("USDT") else sym
            rows.append({"base": base, "price": o["price"], "change_pct": o["change_pct"]})
        if not rows:
            return None
        lines = [f"🐝 <b>MAJORS — SESSION REPORT</b>\n{DIVIDER}"]
        for r in rows:
            emoji = "🟢" if r["change_pct"] >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>{esc(r['base'])}</b>  <code>{fmt_price(r['price'])}</code>  "
                f"({r['change_pct']:+.2f}%)"
            )
        note = self._narrative(rows)
        if note:
            lines.append(f"{DIVIDER}\n🧠 {esc(note)}")
        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
