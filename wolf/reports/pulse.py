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


class MarketPulse:
    def __init__(self, client, symbols: Sequence[str] = DEFAULT_PULSE,
                 interval: str = "1h", tz: str = "UTC") -> None:
        self._client = client
        self._symbols = list(symbols)
        self._interval = interval
        self._tz = tz

    def _bias(self, symbol: str) -> Optional[str]:
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
        return f"{emoji} <b>{esc(base)}</b>  {bias}  · RSI {rsi:.0f}"

    def build(self) -> Optional[str]:
        lines = []
        for sym in self._symbols:
            row = self._bias(sym)
            if row:
                lines.append(row)
        if not lines:
            return None
        return (f"📚 <b>MARKET PULSE</b>\n{DIVIDER}\n" + "\n".join(lines)
                + f"\n🕐 {now(self._tz)}")
