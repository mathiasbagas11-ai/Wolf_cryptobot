"""Flow-intelligence report → 🗞 News topic.

Mimics the on-chain "flow intelligence" thread style (BTC flow → stablecoin dry
powder → chain rotation → token picks/skips → conclusion) using only free data
(CoinGecko + DefiLlama). The deterministic :func:`~wolf.flow.brief.build_brief`
does the analysis; this reporter *renders* it — preferring an LLM narrator
(DeepSeek writes the prose) and falling back to a rule-based template when no AI
client is configured, so the report always works.

The numbers always come from the brief; the LLM only phrases them, and its
output is HTML-escaped before sending.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from wolf.ai.base import LLMClient, NullLLMClient
from wolf.flow.brief import (
    FlowBrief,
    Pick,
    TokenView,
    build_brief,
    build_token_view,
)
from wolf.flow.coingecko import CoinGeckoClient, TokenMetrics
from wolf.flow.defillama import DefiLlamaClient
from wolf.flow.hyperliquid import HyperliquidPerps
from wolf.flow.sentiment import SentimentClient
from wolf.textfmt import DIVIDER, esc, fmt_price, fmt_usd, now

log = logging.getLogger("wolf.reports")

_NARRATOR_SYSTEM = (
    "Lu analis crypto on-chain. Tulis ulang DATA flow intelligence di bawah jadi "
    "thread gaya Telegram berbahasa Indonesia gaul-tapi-tajam, PERSIS gaya ini:\n"
    "- Struktur: 1/ BTC & MARKET (sebut Fear & Greed + Coinbase premium = demand "
    "institusi US; fear + premium positif = sinyal contrarian), 2/ STABLECOIN "
    "(dry powder), 3/ CHAIN ROTATION, "
    "4/ TOKEN PICKS (ranked #1/#2/#3 — tiap pick sebut Mcap, Price 24h, % dari ATH, "
    "Liquidity percentile, Funding signal, FDV/MC, Quant score, thesis singkat & "
    "entry zone), 5/ WATCHLIST, 6/ SKIP (+ alasannya), 7/ KESIMPULAN + STRATEGI.\n"
    "- Pakai emoji (🟢🔥📈✅❌⚠️🥇🥈🥉👀), kalimat pendek nan tegas, sebut angkanya.\n"
    "- Funding BULLISH = shorts crowded (bahan bakar squeeze); BEARISH = longs overheated.\n"
    "- WAJIB cuma pakai angka dari DATA. JANGAN ngarang metrik (mis. whale wallet / "
    "netflow) yang nggak ada di DATA.\n"
    "- Tutup dengan 'NFA — DYOR'.\n"
    "- Output teks polos saja: TANPA tag HTML/markdown."
)

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


class FlowReporter:
    def __init__(
        self,
        coingecko: Optional[CoinGeckoClient] = None,
        defillama: Optional[DefiLlamaClient] = None,
        sentiment: Optional[SentimentClient] = None,
        hyperliquid: Optional[HyperliquidPerps] = None,
        narrator: Optional[LLMClient] = None,
        market_client=None,
        *,
        markets_limit: int = 60,
        max_picks: int = 3,
        max_skips: int = 4,
        max_watch: int = 2,
        quote: str = "USDT",
        tz: str = "UTC",
    ) -> None:
        self._cg = coingecko or CoinGeckoClient()
        self._llama = defillama or DefiLlamaClient()
        self._sentiment = sentiment or SentimentClient()
        self._hl = hyperliquid or HyperliquidPerps()
        self._narrator = narrator or NullLLMClient()
        self._market = market_client   # exchange client → funding fallback (optional)
        self._markets_limit = markets_limit
        self._max_picks = max_picks
        self._max_skips = max_skips
        self._max_watch = max_watch
        self._quote = quote
        self._tz = tz

    # ── orchestration ──────────────────────────────────────────────────
    def gather(self) -> FlowBrief:
        markets = self._cg.top_markets(limit=self._markets_limit)
        global_metrics = self._cg.global_data()
        chains = self._llama.chain_activity()
        stablecoin = self._llama.stablecoin_supply()
        fear_greed = self._sentiment.fear_greed()
        coinbase_premium = self._sentiment.coinbase_premium()
        brief = build_brief(
            markets, global_metrics, chains, stablecoin,
            fear_greed=fear_greed, coinbase_premium=coinbase_premium,
            max_picks=self._max_picks, max_skips=self._max_skips, max_watch=self._max_watch,
        )
        self._enrich_funding(brief.picks + brief.watchlist)
        return brief

    def _enrich_funding(self, picks: list[Pick]) -> None:
        """Fill funding + open interest per pick: Hyperliquid first (one snapshot,
        wide coverage + OI), then the exchange perp as a funding fallback."""
        for p in picks:
            try:
                p.funding_rate = self._hl.funding_rate(p.symbol)
                p.open_interest_usd = self._hl.open_interest_usd(p.symbol)
            except Exception:
                log.debug("hyperliquid lookup failed for %s", p.symbol)
            if p.funding_rate is None and self._market is not None:
                try:
                    p.funding_rate = self._market.get_funding_rate(f"{p.symbol}{self._quote}")
                except Exception:  # funding is optional — never break the report
                    log.debug("funding lookup failed for %s", p.symbol)

    def build(self) -> Optional[str]:
        brief = self.gather()
        if not brief.has_content:
            log.debug("Flow report: no data, skipping")
            return None
        body = self._narrate(brief) or self._template(brief)
        return f"{body}\n{DIVIDER}\n🕐 {now(self._tz)}"

    # ── single-token deep dive (bull vs bear) ──────────────────────────
    def build_token(self, symbol: str) -> Optional[str]:
        """Honest contrarian deep-dive for one token, ENA-thread style.

        Pulls the token's CoinGecko metrics + Hyperliquid funding/OI, derives
        bull and bear factors, then writes them up (LLM if available, else
        template). Returns ``None`` if the token isn't found.
        """
        sym = symbol.upper().strip()
        token = self._find_token(sym)
        if token is None:
            log.debug("Flow deep-dive: %s not found", sym)
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
        except Exception:
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

    # ── LLM narration (preferred) ──────────────────────────────────────
    def _narrate(self, brief: FlowBrief) -> str:
        if not self._narrator.available:
            return ""
        try:
            text = self._narrator.complete(
                _NARRATOR_SYSTEM, _brief_payload(brief), max_tokens=1200
            )
        except Exception:  # narration must never break the scheduled job
            log.exception("Flow narrator failed — using template")
            return ""
        text = (text or "").strip()
        return f"🧠 <b>FLOW INTELLIGENCE</b>\n{DIVIDER}\n{esc(text)}" if text else ""

    # ── deterministic template (fallback) ──────────────────────────────
    def _template(self, b: FlowBrief) -> str:
        lines = [f"🧠 <b>FLOW INTELLIGENCE</b>\n{DIVIDER}"]

        if b.btc is not None or b.fear_greed is not None or b.coinbase_premium is not None:
            lines.append("<b>1/ BTC &amp; MARKET</b>")
            if b.btc is not None:
                arrow = "🟢" if b.btc.market_cap_change_24h >= 0 else "🔴"
                lines.append(f"{arrow} Total mcap {b.btc.market_cap_change_24h:+.1f}% (24h) · "
                             f"BTC dominance {b.btc.btc_dominance:.1f}%")
            if b.fear_greed is not None:
                fg = b.fear_greed
                mood = "😱" if fg.is_fear else ("🤑" if fg.is_greed else "😐")
                lines.append(f"{mood} Fear &amp; Greed {fg.value} ({esc(fg.classification)})")
            if b.coinbase_premium is not None:
                cb = b.coinbase_premium
                tag = {"ACCUMULATION": "🏦 institusi US akumulasi",
                       "DISTRIBUTION": "🔻 institusi US distribusi"}.get(cb.signal, "netral")
                lines.append(f"🏦 Coinbase premium {cb.premium_pct:+.2f}% — {tag}")

        if b.stablecoin is not None:
            s = b.stablecoin
            sign = "numpuk 🔥" if s.change_7d_pct >= 0 else "berkurang 📉"
            lines.append("\n<b>2/ STABLECOIN — dry powder</b>")
            lines.append(f"💵 Total supply {fmt_usd(s.total_usd)} ({s.change_7d_pct:+.1f}% / 7h) — {sign}")
            lines.append("Cash = amunisi buat beli dip." if s.change_7d_pct >= 0
                         else "Dry powder lagi dilepas ke market.")

        if b.chains:
            lines.append("\n<b>3/ CHAIN ROTATION — kemana modal mengalir</b>")
            for c in b.chains[:3]:
                hot = "🔥" if c.change_1d > 0 else "📉"
                lines.append(f"{hot} {esc(c.label)}: DEX vol {fmt_usd(c.dex_volume_24h)} ({c.change_1d:+.1f}%)")

        if b.picks:
            lines.append("\n<b>4/ TOKEN PICKS</b>")
            medals = ["🥇", "🥈", "🥉"]
            for i, p in enumerate(b.picks):
                medal = medals[i] if i < len(medals) else "•"
                lines.append(f"{medal} <b>#{i + 1} — ${esc(p.symbol)}</b> ({esc(p.name)})")
                lines.append(f"   💰 mcap {fmt_usd(p.market_cap)} · 💧 liquidity {p.liquidity_pctile:.0f} pctile")
                ath = f" · 📉 {p.ath_change_pct:.0f}% dari ATH" if p.ath_change_pct <= -1 else ""
                lines.append(f"   Price 24h {p.change_24h:+.1f}%{ath}")
                lines.append(f"   🎯 Quant {p.quant_score}/100 · {esc(_quant_line(p))}")
                lines.append(f"   ✅ {esc(p.entry_note)}")

        if b.watchlist:
            lines.append("\n<b>👀 WATCHLIST</b>")
            for p in b.watchlist:
                lines.append(f"👀 <b>${esc(p.symbol)}</b> — {p.change_24h:+.1f}% 24h · "
                             f"liquidity {p.liquidity_pctile:.0f} pctile · Quant {p.quant_score}/100")

        if b.skips:
            lines.append("\n<b>❌ SKIP — dan kenapa</b>")
            for sk in b.skips:
                lines.append(f"❌ <b>${esc(sk.symbol)}</b> — {esc(sk.reason)}")

        lines.append(f"\n<b>📌 KESIMPULAN: {esc(b.stance)}</b>")
        lines.append(esc(b.conclusion))
        lines.append("\n<i>NFA — DYOR. Data: CoinGecko + DefiLlama</i>")
        return "\n".join(lines)


def _quant_line(p: Pick) -> str:
    """Compact 'Funding X · FDV/MC Yx · turnover' quant summary for a pick."""
    parts = []
    sig = p.funding_signal
    if sig is not None:
        parts.append(f"Funding {sig}")
    if p.fdv_mc is not None:
        parts.append(f"FDV/MC {p.fdv_mc:.1f}x")
    parts.append(f"turnover {(p.vol_mc or 0) * 100:.0f}% mcap")
    if p.open_interest_usd:
        parts.append(f"OI {fmt_usd(p.open_interest_usd)}")
    return " · ".join(parts)


def _pick_payload(p: Pick) -> dict:
    return {
        "symbol": p.symbol, "name": p.name, "price": p.price,
        "change_24h_pct": round(p.change_24h, 2), "market_cap_usd": round(p.market_cap),
        "fdv_mc": round(p.fdv_mc, 2) if p.fdv_mc else None,
        "pct_from_ath": round(p.ath_change_pct, 1),
        "liquidity_percentile": round(p.liquidity_pctile, 1),
        "funding_rate_pct": round(p.funding_rate, 4) if p.funding_rate is not None else None,
        "funding_signal": p.funding_signal,
        "open_interest_usd": round(p.open_interest_usd) if p.open_interest_usd else None,
        "quant_score": p.quant_score,
        "entry_note": p.entry_note,
        "reasons": p.reasons,
    }


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


def _brief_payload(b: FlowBrief) -> str:
    """Compact JSON of the brief for the narrator (numbers it must stick to)."""
    data = {
        "btc_market": None if b.btc is None else {
            "total_mcap_change_24h_pct": round(b.btc.market_cap_change_24h, 2),
            "btc_dominance_pct": round(b.btc.btc_dominance, 2),
        },
        "fear_greed": None if b.fear_greed is None else {
            "value": b.fear_greed.value, "classification": b.fear_greed.classification,
        },
        "coinbase_premium": None if b.coinbase_premium is None else {
            "premium_pct": round(b.coinbase_premium.premium_pct, 3),
            "signal": b.coinbase_premium.signal,
        },
        "stablecoin_dry_powder": None if b.stablecoin is None else {
            "total_usd": round(b.stablecoin.total_usd),
            "change_1d_pct": round(b.stablecoin.change_1d_pct, 2),
            "change_7d_pct": round(b.stablecoin.change_7d_pct, 2),
        },
        "chain_rotation": [
            {"chain": c.label, "dex_volume_24h_usd": round(c.dex_volume_24h),
             "change_1d_pct": round(c.change_1d, 1)}
            for c in b.chains[:5]
        ],
        "token_picks": [_pick_payload(p) for p in b.picks],
        "watchlist": [_pick_payload(p) for p in b.watchlist],
        "skip": [{"symbol": s.symbol, "reason": s.reason} for s in b.skips],
        "stance": b.stance,
        "conclusion": b.conclusion,
    }
    return "DATA:\n" + json.dumps(data, ensure_ascii=False)
