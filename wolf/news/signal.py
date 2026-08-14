"""News-driven signal scanner.

Scans fresh news headlines for coin mentions and generates short-lived NEWS
signals when strong bullish or bearish keywords are found. These complement
the technical detectors — they fire on catalysts that precede chart moves.

Rules:
- Only signals coins in the configured universe.
- Requires >= 2 matching sentiment keywords to reduce noise.
- MOMENTUM_NOW entry mode (news trades must enter immediately).
- ATR-based TP/SL from the current 15m candle.
- Dedup handled by the Tracker (same dedup_minutes window).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from wolf.detectors.base import SignalCandidate, build_targets
from wolf.models import EntryMode

log = logging.getLogger("wolf.news.signal")

_COIN_MAP: dict[str, str] = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "bnb": "BNBUSDT", "binance coin": "BNBUSDT",
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
    "cardano": "ADAUSDT", "ada": "ADAUSDT",
    "avalanche": "AVAXUSDT", "avax": "AVAXUSDT",
    "chainlink": "LINKUSDT", "link": "LINKUSDT",
    "ton": "TONUSDT",
    "sui": "SUIUSDT",
    "aptos": "APTUSDT", "apt": "APTUSDT",
    "arbitrum": "ARBUSDT", "arb": "ARBUSDT",
    "optimism": "OPUSDT",
    "injective": "INJUSDT", "inj": "INJUSDT",
}

_BULL_WORDS = (
    "surge", "surges", "surging", "rally", "rallies", "rallying",
    "rise", "rises", "rising", "soar", "soars", "soaring",
    "break", "breaks", "breaking", "all-time high", "ath",
    "bullish", "adoption", "launch", "partnership", "upgrade",
    "approve", "approves", "approved", "buy",
)
_BEAR_WORDS = (
    "plunge", "plunges", "plunging", "crash", "crashes", "crashing",
    "fall", "falls", "falling", "drop", "drops", "dropping",
    "bearish", "ban", "banned", "banning", "hack", "hacked",
    "exploit", "lawsuit", "fraud", "collapse", "fear",
    "sell-off", "selloff", "regulatory", "restrict",
)

_MIN_KEYWORD_HITS = 2


def _extract_coins(title: str, universe: set) -> list[str]:
    text = title.lower()
    found: list[str] = []
    seen: set = set()
    for name, sym in _COIN_MAP.items():
        if sym not in universe:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", text) and sym not in seen:
            found.append(sym)
            seen.add(sym)
    return found


def _sentiment(title: str) -> tuple[int, int]:
    text = title.lower()
    bull = sum(1 for w in _BULL_WORDS if w in text)
    bear = sum(1 for w in _BEAR_WORDS if w in text)
    return bull, bear


class NewsSignalScanner:
    """Turns fresh news items into SignalCandidates."""

    def __init__(self, client, universe: set, interval: str = "15m", candle_limit: int = 100) -> None:
        self._client = client
        self._universe = universe
        self._interval = interval
        self._candle_limit = candle_limit

    def scan(self, news_items) -> list[SignalCandidate]:
        results: list[SignalCandidate] = []
        for item in news_items:
            results.extend(self._process(item))
        return results

    def _process(self, item) -> list[SignalCandidate]:
        coins = _extract_coins(item.title, self._universe)
        if not coins:
            return []
        bull_hits, bear_hits = _sentiment(item.title)
        if bull_hits >= _MIN_KEYWORD_HITS and bull_hits > bear_hits:
            direction, hits = "LONG", bull_hits
        elif bear_hits >= _MIN_KEYWORD_HITS and bear_hits > bull_hits:
            direction, hits = "SHORT", bear_hits
        else:
            return []
        score = min(50 + hits * 15, 95)
        return [c for c in (self._build(sym, direction, score, item.title) for sym in coins) if c]

    def _build(self, symbol: str, direction: str, score: int, headline: str) -> Optional[SignalCandidate]:
        try:
            candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        except Exception:
            return None
        if not candles or len(candles) < 20:
            return None
        from wolf import indicators as ind
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        atr_vals = ind.atr(highs, lows, closes, 14)
        atr = atr_vals[-1] if atr_vals else closes[-1] * 0.01
        entry = closes[-1]
        is_long = direction == "LONG"
        sl, tp, tps = build_targets(entry, atr, is_long, sl_mult=1.5, tp_mults=(2.5, 4.0))
        bull_h, bear_h = _sentiment(headline)
        return SignalCandidate(
            symbol=symbol,
            signal_type="NEWS",
            direction=direction,
            entry_price=entry,
            tp=tp,
            sl=sl,
            score=score,
            strategy="NEWS",
            reasons=[
                f"News: {headline[:90]}",
                f"Sentiment: {bull_h} bull / {bear_h} bear keywords",
            ],
            confluence_level="NEWS",
            entry_mode=EntryMode.MOMENTUM_NOW.value,
            tps=tps,
        )
