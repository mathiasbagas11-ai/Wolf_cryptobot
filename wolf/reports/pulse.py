"""Market pulse → 📚 Market Update topic.

A periodic read on overall market bias from BTC and ETH: trend (EMA50 vs EMA200
proxy via EMA20/EMA50), momentum (RSI) and 24h drift. Lightweight — a couple of
klines requests.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf.textfmt import DIVIDER, esc, now

log = logging.getLogger("wolf.reports")

DEFAULT_PULSE = ("BTCUSDT", "ETHUSDT")


_PULSE_SYSTEM = (
    "You are a crypto market strategist. Given BTC/ETH trend, momentum (RSI) and "
    "funding readings, write a 1-2 sentence read on overall market bias and call "
    "out any bias SHIFT (e.g. structure flipping, momentum cooling, funding "
    "stress). Be concrete and concise. No preamble, no disclaimers."
)


class MarketPulse:
    def __init__(self, client, symbols: Sequence[str] = DEFAULT_PULSE,
                 interval: str = "1h", tz: str = "UTC", llm=None) -> None:
        self._client = client
        self._symbols = list(symbols)
        self._interval = interval
        self._tz = tz
        self._llm = llm

    def _read(self, symbol: str) -> Optional[dict]:
        candles = self._client.get_klines(symbol, self._interval, 120)
        if len(candles) < 60:
            return None
        closes = ind.closes(candles)
        price = closes[-1]
        ema20 = ind.ema(closes, 20)[-1]
        ema50 = ind.ema(closes, 50)[-1]
        rsi = ind.rsi(closes, 14)
        if math.isnan(rsi):
            return None
        if price > ema20 > ema50:
            bias, emoji = "BULLISH", "🟢"
        elif price < ema20 < ema50:
            bias, emoji = "BEARISH", "🔴"
        else:
            bias, emoji = "NEUTRAL", "⚪"
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        funding = None
        try:
            funding = self._client.get_funding_rate(symbol)
        except Exception:  # funding is best-effort context only
            funding = None
        return {
            "base": base, "bias": bias, "emoji": emoji,
            "rsi": rsi, "price": price, "ema20": ema20, "ema50": ema50,
            "funding": funding,
        }

    def _narrative(self, reads: list[dict]) -> str:
        if self._llm is None or not getattr(self._llm, "available", False):
            return ""
        facts = "\n".join(
            f"{r['base']}: {r['bias']} (price {r['price']:.4g}, EMA20 {r['ema20']:.4g}, "
            f"EMA50 {r['ema50']:.4g}, RSI {r['rsi']:.0f}"
            + (f", funding {r['funding']:+.3f}%" if r['funding'] is not None else "")
            + ")"
            for r in reads
        )
        try:
            return self._llm.complete(_PULSE_SYSTEM, facts, max_tokens=200).strip()
        except Exception:
            log.warning("Market pulse narrative failed", exc_info=True)
            return ""

    def build(self) -> Optional[str]:
        reads = [r for r in (self._read(s) for s in self._symbols) if r]
        if not reads:
            return None
        rows = [f"{r['emoji']} <b>{esc(r['base'])}</b>  {r['bias']}  · RSI {r['rsi']:.0f}" for r in reads]
        body = f"📚 <b>MARKET PULSE</b>\n{DIVIDER}\n" + "\n".join(rows)
        note = self._narrative(reads)
        if note:
            body += f"\n{DIVIDER}\n🧠 {esc(note)}"
        return body + f"\n🕐 {now(self._tz)}"
