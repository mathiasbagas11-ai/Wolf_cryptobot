"""Single-token deep dive — an on-demand, bull-vs-bear read of one coin.

Split out of :mod:`wolf.reports.flow` when the periodic digest became a
StateStore reader. The two answer different questions on different schedules:
Flow Intelligence is a market-wide digest on a timer, this is one coin, fetched
when somebody asks for it (``POST /flow/{symbol}``). Fetching inside ``build_token``
is correct here — the request *is* the trigger, and there is no second consumer
of the result to disagree with.

The honesty constraint is the point of the format: every bull case is printed
next to the bear case that undercuts it. The numbers come from
:func:`~wolf.flow.brief.build_token_view`; the LLM (when configured) only phrases
them, and its output is HTML-escaped before sending.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from wolf.ai.base import LLMClient, NullLLMClient
from wolf.flow.brief import TokenView, build_token_view
from wolf.flow.coingecko import CoinGeckoClient, TokenMetrics
from wolf.flow.hyperliquid import HyperliquidPerps
from wolf.textfmt import DIVIDER, esc, fmt_price, fmt_usd, now

log = logging.getLogger("wolf.reports")

_DEEPDIVE_SYSTEM = (
    "Lu analis crypto yang JUJUR (bukan shiller). Dari DATA satu token di bawah, "
    "tulis deep-dive gaya thread Telegram Indonesia, struktur ini:\n"
    "- Pembuka 1-2 kalimat: kenapa token ini menarik / kontroversial.\n"
    "- 'Sisi BULLISH:' bullet kelebihannya.\n"
    "- 'Sisi BEARISH (gw ga mau cuma shill):' bullet risikonya — JANGAN disoftenkan.\n"
    "- 'Kondisi sekarang:' harga, mcap, % dari ATH, funding, OI.\n"
    "- 'Cara gw main:' playbook (conviction vs momentum, sizing/DCA, leverage, horizon).\n"
    "- Pakai emoji (🟢🔴✅❌⚠️🎯💰📉📊), sebut angkanya.\n"
    "- WAJIB cuma pakai angka dari DATA. JANGAN ngarang netflow/whale yang nggak ada.\n"
    "- Tutup 'NFA — DYOR'. Output teks polos: TANPA tag HTML/markdown."
)


class TokenDeepDive:
    """Builds the per-token bull/bear card."""

    def __init__(
        self,
        coingecko: Optional[CoinGeckoClient] = None,
        hyperliquid: Optional[HyperliquidPerps] = None,
        narrator: Optional[LLMClient] = None,
        market_client=None,
        *,
        markets_limit: int = 60,
        quote: str = "USDT",
        tz: str = "UTC",
    ) -> None:
        self._cg = coingecko or CoinGeckoClient()
        self._hl = hyperliquid or HyperliquidPerps()
        self._narrator = narrator or NullLLMClient()
        self._market = market_client   # exchange client → funding fallback (optional)
        self._markets_limit = markets_limit
        self._quote = quote
        self._tz = tz

    def build_token(self, symbol: str) -> Optional[str]:
        """Honest deep-dive for one token, or ``None`` if it is not listed."""
        sym = symbol.upper().strip()
        token = self._find_token(sym)
        if token is None:
            log.debug("Deep dive: %s not found", sym)
            return None
        funding = self._hl.funding_rate(sym)
        if funding is None and self._market is not None:
            funding = self._market.get_funding_rate(f"{sym}{self._quote}")
        oi = self._hl.open_interest_usd(sym)
        view = build_token_view(token, funding=funding, open_interest_usd=oi)
        body = self._narrate_token(view) or self._template_token(view)
        return f"{body}\n{DIVIDER}\n🕐 {now(self._tz)}"

    def _find_token(self, sym: str) -> Optional[TokenMetrics]:
        for t in self._cg.top_markets(limit=max(self._markets_limit, 250)):
            if t.symbol == sym:
                return t
        return None

    def _narrate_token(self, view: TokenView) -> str:
        if not self._narrator.available:
            return ""
        try:
            text = self._narrator.complete(_DEEPDIVE_SYSTEM, _token_payload(view), max_tokens=1100)
        except Exception:  # narration must never break an on-demand request
            log.exception("Deep-dive narrator failed — using template")
            return ""
        text = (text or "").strip()
        return (f"🔬 <b>DEEP DIVE — ${esc(view.symbol)}</b>\n{DIVIDER}\n{esc(text)}"
                if text else "")

    def _template_token(self, v: TokenView) -> str:
        lines = [f"🔬 <b>DEEP DIVE — ${esc(v.symbol)}</b> ({esc(v.name)})\n{DIVIDER}"]
        lines.append(f"💰 Harga ${fmt_price(v.price)} ({v.change_24h:+.1f}%) · mcap {fmt_usd(v.market_cap)}")
        if v.ath_change_pct <= -1:
            lines.append(f"📉 {v.ath_change_pct:.0f}% dari ATH")
        if v.open_interest_usd:
            lines.append(f"📊 Open interest {fmt_usd(v.open_interest_usd)}")
        lines.append(f"🎯 Conviction score {v.score}/100 · stance: {esc(v.stance)}")

        lines.append("\n<b>✅ Sisi BULLISH</b>")
        lines += [f"🟢 {esc(b)}" for b in v.bull] or ["🟢 —"]
        lines.append("\n<b>❌ Sisi BEARISH (jujur, bukan shill)</b>")
        lines += [f"🔴 {esc(b)}" for b in v.bear] or ["🔴 —"]

        lines.append("\n<b>📌 Cara main</b>")
        lines += [f"• {esc(s)}" for s in v.playbook]
        lines.append("\n<i>NFA — DYOR. Data: CoinGecko + Hyperliquid</i>")
        return "\n".join(lines)


def _token_payload(v: TokenView) -> str:
    data = {
        "symbol": v.symbol, "name": v.name, "price": v.price,
        "change_24h_pct": round(v.change_24h, 2), "market_cap_usd": round(v.market_cap),
        "fdv_mc": round(v.fdv_mc, 2) if v.fdv_mc else None,
        "pct_from_ath": round(v.ath_change_pct, 1),
        "funding_rate_pct": round(v.funding_rate, 4) if v.funding_rate is not None else None,
        "open_interest_usd": round(v.open_interest_usd) if v.open_interest_usd else None,
        "conviction_score": v.score, "stance": v.stance,
        "bull_factors": v.bull, "bear_factors": v.bear, "playbook": v.playbook,
    }
    return "DATA:\n" + json.dumps(data, ensure_ascii=False)
