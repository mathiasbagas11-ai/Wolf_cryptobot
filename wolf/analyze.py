"""On-demand coin analysis — powers the ``/analyze`` Telegram command.

Pulls one symbol's candles and produces a compact technical read: trend/regime,
momentum (RSI/MACD), volatility (ATR), volume, the best detector setup right now
(if any), the symbol's learned track record, and — when the AI layer is on — a
Bull/Bear verdict. Pure orchestration over the same components the screener uses
(:class:`~wolf.indicator_cache.CandleFeatures`, the detectors, the regime read),
so the analysis can never drift from what the bot actually trades.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

from wolf.detectors.base import Detector, SignalCandidate
from wolf.indicator_cache import CandleFeatures
from wolf.regime import trend_bias
from wolf.textfmt import DIVIDER, esc, fmt_price, now

log = logging.getLogger("wolf.analyze")


def normalize_symbol(raw: str) -> str:
    """``btc`` / ``btc-usdt`` / ``BTCUSDT`` -> ``BTCUSDT``."""
    s = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if not s:
        return ""
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if s.endswith(quote) and len(s) > len(quote):
            return s
    return f"{s}USDT"


class AnalyzeService:
    def __init__(self, client, detectors: Sequence[Detector], context_provider=None,
                 learning=None, validator=None, interval: str = "15m",
                 candle_limit: int = 150, tz: str = "UTC") -> None:
        self._client = client
        self._detectors = list(detectors)
        self._context_provider = context_provider
        self._learning = learning
        self._validator = validator
        self._interval = interval
        self._candle_limit = candle_limit
        self._tz = tz

    def _best_candidate(self, symbol, candles, context, features) -> Optional[SignalCandidate]:
        best = None
        for det in self._detectors:
            try:
                cand = det.evaluate(symbol, candles, context, features)
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            if cand and (best is None or cand.score > best.score):
                best = cand
        return best

    def latest_setup(self, symbol: str):
        """Return the best detector candidate for ``symbol`` now, or None.

        Used by ``/calc`` to size a trade plan against the current setup.
        """
        candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        if len(candles) < 60:
            return None
        try:
            f = CandleFeatures.build(candles)
        except Exception:
            return None
        context = None
        if self._context_provider is not None:
            try:
                context = self._context_provider.build(symbol)
            except (ValueError, KeyError, TypeError):
                context = None
        return self._best_candidate(symbol, candles, context, f)

    def analyze(self, raw_symbol: str) -> str:
        symbol = normalize_symbol(raw_symbol)
        if not symbol:
            return "⚠️ Usage: <code>/analyze BTC</code>"
        candles = self._client.get_klines(symbol, self._interval, self._candle_limit)
        if len(candles) < 60:
            return f"⚠️ Not enough data for <b>{esc(symbol)}</b> (is the ticker right?)"

        try:
            f = CandleFeatures.build(candles)
        except Exception:
            return f"⚠️ Could not compute indicators for <b>{esc(symbol)}</b>"
        regime = trend_bias(candles)

        def _fmt(x, suffix="", nd=1):
            return f"{x:.{nd}f}{suffix}" if isinstance(x, (int, float)) and not math.isnan(x) else "—"

        trend = "—"
        if not math.isnan(f.ema20_last) and not math.isnan(f.ema50_last):
            trend = "EMA20>EMA50 ▲" if f.ema20_last > f.ema50_last else "EMA20<EMA50 ▼"

        lines = [
            f"🔎 <b>ANALYSIS · {esc(symbol)}</b>\n{DIVIDER}",
            f"💵 Price <code>{fmt_price(f.price)}</code>",
            f"🌡 Regime <b>{esc(regime)}</b> · Trend {esc(trend)}",
            f"📊 RSI {_fmt(f.rsi, nd=0)} · MACD {'＋' if f.macd_hist > 0 else '－'} · Vol {_fmt(f.vol_ratio, 'x')}",
            f"📐 ATR {fmt_price(f.atr)} ({_fmt(f.atr / f.price * 100 if f.price else float('nan'), '%')})",
        ]

        context = None
        if self._context_provider is not None:
            try:
                context = self._context_provider.build(symbol)
            except (ValueError, KeyError, TypeError):
                context = None
        if context is not None and getattr(context, "funding_rate", None) is not None:
            lines.append(f"💸 Funding {context.funding_rate:+.3f}%")

        cand = self._best_candidate(symbol, candles, context, f)
        lines.append(DIVIDER)
        if cand:
            emoji = "🟢" if cand.direction == "LONG" else "🔴"
            lines.append(f"{emoji} <b>Setup: {esc(cand.strategy)} {esc(cand.direction)}</b> · {cand.score}/100")
            lines.append(
                f"   Entry <code>{fmt_price(cand.entry_price)}</code> · "
                f"TP <code>{fmt_price(cand.tp)}</code> · SL <code>{fmt_price(cand.sl)}</code>"
            )
            for r in cand.reasons[:3]:
                lines.append(f"   • {esc(r)}")
            if self._validator is not None:
                try:
                    verdict = self._validator.validate(cand, context, candles=candles)
                    if verdict.decision and verdict.decision != "ABSTAIN":
                        lines.append(f"🧠 AI: {esc(verdict.decision)} {verdict.confidence}% — {esc(verdict.rationale)}")
                except Exception:
                    log.debug("AI analysis failed for %s", symbol, exc_info=True)
        else:
            lines.append("➖ No qualifying setup right now.")

        if self._learning is not None:
            edge = self._learning.symbol_edge(symbol)
            if edge:
                lines.append(DIVIDER)
                lines.append(
                    f"🧠 Memory: {edge['win_rate']:.0f}% win over {edge['trades']} "
                    f"({edge['avg_pnl']:+.2f}% avg, {edge['avg_r']:+.2f}R)"
                )
                if symbol in self._learning.snapshot().get("blacklist", []):
                    lines.append("⛔ Currently blacklisted (poor track record)")

        lines.append(f"🕐 {now(self._tz)}")
        return "\n".join(lines)
