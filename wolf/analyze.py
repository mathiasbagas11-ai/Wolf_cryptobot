"""On-demand coin analysis — powers the ``/analyze`` Telegram command.

Pulls one symbol's candles and produces a compact technical read: trend/regime
(ADX), momentum (RSI/MACD), volatility (ATR/Bollinger), volume, the best detector
setup right now (if any), the symbol's learned track record, and — when the AI
layer is on — a Bull/Bear verdict. Pure orchestration over the same components
the screener uses, so the analysis can never drift from what the bot actually
trades.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf.detectors.base import Detector, SignalCandidate
from wolf.regime import detect_regime
from wolf.textfmt import DIVIDER, esc, fmt_price, now

log = logging.getLogger("wolf.analyze")


def normalize_symbol(raw: str) -> str:
    """``btc`` / ``btc-usdt`` / ``BTCUSDT`` -> ``BTCUSDT``."""
    s = "".join(ch for ch in raw.upper() if ch.isalnum())
    if not s:
        return ""
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if s.endswith(quote) and len(s) > len(quote):
            return s
    return f"{s}USDT"


class AnalyzeService:
    def __init__(
        self,
        client,
        detectors: Sequence[Detector],
        context_provider=None,
        learning=None,
        regime_settings=None,
        validator=None,
        interval: str = "15m",
        candle_limit: int = 150,
        tz: str = "UTC",
    ) -> None:
        self._client = client
        self._detectors = list(detectors)
        self._context_provider = context_provider
        self._learning = learning
        self._regime = regime_settings
        self._validator = validator
        self._interval = interval
        self._candle_limit = candle_limit
        self._tz = tz

    def _best_candidate(self, symbol, candles, context) -> Optional[SignalCandidate]:
        best = None
        for det in self._detectors:
            try:
                cand = det.evaluate(symbol, candles, context)
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            if cand and (best is None or cand.score > best.score):
                best = cand
        return best

    def analyze(self, raw_symbol: str) -> str:
        symbol = normalize_symbol(raw_symbol)
        if not symbol:
            return "⚠️ Usage: <code>/analyze BTC</code>"
        candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        if len(candles) < 60:
            return f"⚠️ Not enough data for <b>{esc(symbol)}</b> (is the ticker right?)"

        closes = ind.closes(candles)
        price = closes[-1]
        rsi = ind.rsi(closes, 14)
        _, _, hist = ind.macd(closes)
        atr = ind.atr(candles, 14)
        vr = ind.volume_ratio(candles, 20)
        ema20 = ind.ema(closes, 20)
        ema50 = ind.ema(closes, 50)
        reg = detect_regime(
            candles,
            self._regime.adx_period if self._regime else 14,
            self._regime.adx_trend_min if self._regime else 20.0,
        )

        def _fmt(x, suffix="", nd=1):
            return f"{x:.{nd}f}{suffix}" if isinstance(x, (int, float)) and not math.isnan(x) else "—"

        trend = "—"
        if ema20 and ema50:
            trend = "EMA20>EMA50 ▲" if ema20[-1] > ema50[-1] else "EMA20<EMA50 ▼"

        lines = [
            f"🔎 <b>ANALYSIS · {esc(symbol)}</b>\n{DIVIDER}",
            f"💵 Price <code>{fmt_price(price)}</code>",
            f"🌡 Regime <b>{esc(reg.label)}</b> (ADX {_fmt(reg.adx, nd=0)})",
            f"📈 Trend {esc(trend)}",
            f"📊 RSI {_fmt(rsi, nd=0)} · MACD {'＋' if hist > 0 else '－'} · Vol {_fmt(vr, 'x')}",
            f"📐 ATR {fmt_price(atr)} ({_fmt(atr / price * 100 if price else float('nan'), '%')})",
        ]

        context = None
        if self._context_provider is not None:
            try:
                context = self._context_provider.build(symbol)
            except (ValueError, KeyError, TypeError):
                context = None
        if context is not None and getattr(context, "funding_rate", None) is not None:
            lines.append(f"💸 Funding {context.funding_rate:+.3f}%")

        cand = self._best_candidate(symbol, candles, context)
        lines.append(DIVIDER)
        if cand:
            emoji = "🟢" if cand.direction == "LONG" else "🔴"
            aligned = "" if reg.aligns_with(cand.direction) else " ⚠️ counter-trend"
            lines.append(f"{emoji} <b>Setup: {esc(cand.strategy)} {esc(cand.direction)}</b> · {cand.score}/100{aligned}")
            lines.append(f"   Entry <code>{fmt_price(cand.entry_price)}</code> · TP <code>{fmt_price(cand.tp)}</code> · SL <code>{fmt_price(cand.sl)}</code>")
            for r in cand.reasons[:3]:
                lines.append(f"   • {esc(r)}")
            if self._validator is not None:
                verdict = self._validator.validate(cand, context)
                if verdict.decision and verdict.decision != "ABSTAIN":
                    lines.append(f"🧠 AI: {esc(verdict.decision)} {verdict.confidence}% — {esc(verdict.rationale)}")
        else:
            lines.append("➖ No qualifying setup right now.")

        # Learned track record for this symbol.
        if self._learning is not None:
            snap = self._learning.snapshot().get("symbols", {}).get(symbol)
            if snap and snap.get("trades"):
                lines.append(DIVIDER)
                lines.append(
                    f"🧠 Memory: {snap['win_rate']:.0f}% win over {snap['trades']} "
                    f"({snap['avg_pnl']:+.2f}% avg, {snap['avg_r']:+.2f}R)"
                )
                if symbol in self._learning.snapshot().get("blacklist", []):
                    lines.append("⛔ Currently blacklisted (poor track record)")

        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
