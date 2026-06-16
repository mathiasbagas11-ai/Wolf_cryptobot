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
from wolf.flow.brief import FlowBrief, build_brief
from wolf.flow.coingecko import CoinGeckoClient
from wolf.flow.defillama import DefiLlamaClient
from wolf.textfmt import DIVIDER, esc, fmt_price, fmt_usd, now

log = logging.getLogger("wolf.reports")

_NARRATOR_SYSTEM = (
    "Lu analis crypto on-chain. Tulis ulang DATA flow intelligence di bawah jadi "
    "thread gaya Telegram berbahasa Indonesia gaul-tapi-tajam, PERSIS gaya ini:\n"
    "- Struktur: 1/ BTC & MARKET, 2/ STABLECOIN (dry powder), 3/ CHAIN ROTATION, "
    "4/ TOKEN PICKS, 5/ SKIP (+ alasannya), 6/ KESIMPULAN + STRATEGI.\n"
    "- Pakai emoji (🟢🔥📈✅❌⚠️🥇), kalimat pendek nan tegas, sebut angkanya.\n"
    "- WAJIB cuma pakai angka dari DATA. JANGAN ngarang metrik (mis. whale wallet) "
    "yang nggak ada di DATA.\n"
    "- Tutup dengan 'NFA — DYOR'.\n"
    "- Output teks polos saja: TANPA tag HTML/markdown."
)


class FlowReporter:
    def __init__(
        self,
        coingecko: Optional[CoinGeckoClient] = None,
        defillama: Optional[DefiLlamaClient] = None,
        narrator: Optional[LLMClient] = None,
        *,
        markets_limit: int = 60,
        max_picks: int = 3,
        max_skips: int = 4,
        tz: str = "UTC",
    ) -> None:
        self._cg = coingecko or CoinGeckoClient()
        self._llama = defillama or DefiLlamaClient()
        self._narrator = narrator or NullLLMClient()
        self._markets_limit = markets_limit
        self._max_picks = max_picks
        self._max_skips = max_skips
        self._tz = tz

    # ── orchestration ──────────────────────────────────────────────────
    def gather(self) -> FlowBrief:
        markets = self._cg.top_markets(limit=self._markets_limit)
        global_metrics = self._cg.global_data()
        chains = self._llama.chain_activity()
        stablecoin = self._llama.stablecoin_supply()
        return build_brief(
            markets, global_metrics, chains, stablecoin,
            max_picks=self._max_picks, max_skips=self._max_skips,
        )

    def build(self) -> Optional[str]:
        brief = self.gather()
        if not brief.has_content:
            log.debug("Flow report: no data, skipping")
            return None
        body = self._narrate(brief) or self._template(brief)
        return f"{body}\n{DIVIDER}\n🕐 {now(self._tz)}"

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

        if b.btc is not None:
            arrow = "🟢" if b.btc.market_cap_change_24h >= 0 else "🔴"
            lines.append("<b>1/ BTC &amp; MARKET</b>")
            lines.append(f"{arrow} Total mcap {b.btc.market_cap_change_24h:+.1f}% (24h)")
            lines.append(f"📊 BTC dominance {b.btc.btc_dominance:.1f}%")

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
                lines.append(f"{medal} <b>${esc(p.symbol)}</b> — {esc(p.name)}")
                lines.append(f"   Harga ${fmt_price(p.price)} ({p.change_24h:+.1f}%) · mcap {fmt_usd(p.market_cap)}")
                lines.append("   " + " · ".join(esc(r) for r in p.reasons))

        if b.skips:
            lines.append("\n<b>5/ ❌ SKIP — dan kenapa</b>")
            for sk in b.skips:
                lines.append(f"❌ <b>${esc(sk.symbol)}</b> — {esc(sk.reason)}")

        lines.append(f"\n<b>6/ KESIMPULAN: {esc(b.stance)}</b>")
        lines.append(esc(b.conclusion))
        lines.append("\n<i>NFA — DYOR. Data: CoinGecko + DefiLlama</i>")
        return "\n".join(lines)


def _brief_payload(b: FlowBrief) -> str:
    """Compact JSON of the brief for the narrator (numbers it must stick to)."""
    data = {
        "btc_market": None if b.btc is None else {
            "total_mcap_change_24h_pct": round(b.btc.market_cap_change_24h, 2),
            "btc_dominance_pct": round(b.btc.btc_dominance, 2),
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
        "token_picks": [
            {"symbol": p.symbol, "name": p.name, "price": p.price,
             "change_24h_pct": round(p.change_24h, 2), "market_cap_usd": round(p.market_cap),
             "fdv_mc": round(p.fdv_mc, 2) if p.fdv_mc else None,
             "liquidity_pct_mcap": round((p.vol_mc or 0) * 100, 1), "reasons": p.reasons}
            for p in b.picks
        ],
        "skip": [{"symbol": s.symbol, "reason": s.reason} for s in b.skips],
        "stance": b.stance,
        "conclusion": b.conclusion,
    }
    return "DATA:\n" + json.dumps(data, ensure_ascii=False)
