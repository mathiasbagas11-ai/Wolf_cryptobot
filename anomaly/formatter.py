"""Phase 5 — Output formatter.

Renders the ANOMALY SCANNER section that is appended to the existing 4-hourly
FLOW INTELLIGENCE Telegram message. Its whole reason for existing is the *gate*:
this scanner must be allowed to say "nothing today" rather than manufacture a
pick. A bot that can stay quiet is worth more than one that always has a call.

The gate (in order):

1. **Nothing worth showing** — if no coin scores ≥ ``SHOW_MIN_SCORE`` (55), print
   "Tidak ada anomali terdeteksi. Sabar." and stop. Never force a pick.
2. **Weak-market caution** — if the market verdict is BEARISH/NEUTRAL *and* the
   best score is < ``ENTRY_CONVICTION`` (65), drop to WATCHLIST mode: show the
   coins but withhold entry ladders ("👀 WATCHLIST — belum ada entry").
3. Otherwise render full picks with their entry ladders.

Everything is PAPER MODE — signals are logged for expectancy evaluation, never
auto-executed. The formatter is pure (no network, no clock): it renders the
dicts produced by the scoring (Phase 3) + ladder (Phase 4) stages, with the
market verdict passed in by the caller (mapped from the flow brief's stance via
:func:`simplify_verdict`).
"""

from __future__ import annotations

import html
from typing import Optional

from anomaly.entry import format_price

# ── gate thresholds ────────────────────────────────────────────────────────
SHOW_MIN_SCORE = 55        # below this → not worth surfacing at all
ENTRY_CONVICTION = 65      # below this in a weak market → watchlist, no entries
MAX_DISPLAY = 3            # cap the number of picks shown

WEAK_MARKET = {"BEARISH", "NEUTRAL"}

DIVIDER = "━━━━━━━━━━━━━━━"
_MEDALS = ["🥇", "🥈", "🥉"]

_COMPONENT_LABELS = [
    ("volatility_contraction", "Coil"),
    ("volume_anomaly", "Vol"),
    ("structure_position", "Struct"),
    ("supply_health", "Supply"),
]


def simplify_verdict(stance: Optional[str]) -> str:
    """Map the flow brief's stance to the gate's BULLISH/BEARISH/NEUTRAL verdict.

    Flow stances: RISK-ON / RISK-ON (contrarian) / RISK-OFF / ROTATION / NEUTRAL.
    """
    s = (stance or "").upper()
    if s.startswith("RISK-ON"):
        return "BULLISH"
    if s.startswith("RISK-OFF"):
        return "BEARISH"
    return "NEUTRAL"


def format_anomaly_section(
    scored: list,
    flagged: list,
    *,
    market_verdict: str = "NEUTRAL",
    max_display: int = MAX_DISPLAY,
) -> str:
    """Render the ANOMALY SCANNER section (HTML for Telegram).

    ``scored`` are non-flagged coins from :func:`anomaly.scoring.score_coin`,
    each optionally enriched with ``current_price`` and a ``ladder`` (Phase 4).
    ``flagged`` are the red-flagged coins (rendered as a short exclusion note).
    """
    verdict = (market_verdict or "NEUTRAL").upper()
    candidates = sorted(
        (c for c in scored if not c.get("flagged")),
        key=lambda c: c.get("score", 0), reverse=True,
    )
    surfacing = [c for c in candidates if c.get("score", 0) >= SHOW_MIN_SCORE]

    header = f"🔍 <b>ANOMALY SCANNER</b> — coiling / volume anomaly\n{DIVIDER}"

    # ── GATE 1: nothing worth showing ──
    if not surfacing:
        return (f"{header}\n😴 Tidak ada anomali terdeteksi. Sabar.\n"
                f"{_footer(flagged)}")

    max_score = max(c.get("score", 0) for c in surfacing)
    watchlist_mode = verdict in WEAK_MARKET and max_score < ENTRY_CONVICTION

    picks = surfacing[:max_display]
    lines = [header]

    # ── GATE 2: weak market + low conviction → watchlist, no entries ──
    if watchlist_mode:
        lines.append("👀 <b>WATCHLIST — belum ada entry</b>")
        lines.append(f"<i>Market {_esc(verdict)} &amp; skor tertinggi {max_score} "
                     f"(&lt;{ENTRY_CONVICTION}) — pantau dulu, belum waktunya entry.</i>")
        for i, c in enumerate(picks):
            lines.append(_render_coin(c, i, with_ladder=False))
    else:
        lines.append("🎯 <b>ANOMALY PICKS</b>")
        for i, c in enumerate(picks):
            lines.append(_render_coin(c, i, with_ladder=True))

    lines.append(_footer(flagged))
    return "\n".join(lines)


# ── per-coin rendering ─────────────────────────────────────────────────────
def _render_coin(c: dict, idx: int, *, with_ladder: bool) -> str:
    medal = _MEDALS[idx] if idx < len(_MEDALS) else "•"
    sym = _esc(str(c.get("symbol", "?")))
    name = _esc(str(c.get("name", "")))
    score = int(c.get("score", 0))
    dca = " · 💼 DCA sleeve" if c.get("in_dca_sleeve") else ""

    out = [f"\n{medal} <b>${sym}</b> ({name}) · Score {score}/100{dca}"]
    out.append(f"   📊 {_component_line(c.get('components', {}))}")

    metrics_line = _metrics_line(c.get("metrics", {}))
    if metrics_line:
        out.append(f"   🔎 {metrics_line}")

    price = c.get("current_price")
    ladder = c.get("ladder") or {}
    if with_ladder and ladder.get("l1") is not None:
        out.append(_ladder_lines(ladder, price))
    elif price is not None:
        out.append(f"   💰 Price {format_price(price)}")
    return "\n".join(out)


def _ladder_lines(lad: dict, price) -> str:
    sizes = lad.get("sizes", {})
    price_str = f"Price {format_price(price)} · " if price is not None else ""
    lines = [
        f"   💰 {price_str}Entry ladder:",
        f"     L1 {_pct(sizes.get('l1'))} {format_price(lad.get('l1'))} · "
        f"L2 {_pct(sizes.get('l2'))} {format_price(lad.get('l2'))} · "
        f"L3 {_pct(sizes.get('l3'))} {format_price(lad.get('l3'))}",
        f"   🛑 Invalidation {format_price(lad.get('invalidation'))} · ⚖️ RR {lad.get('rr_ratio', 0)}",
        f"   🎯 TP1 {format_price(lad.get('tp1'))} ({_pct(sizes.get('tp1'))}) · "
        f"TP2 {format_price(lad.get('tp2'))} ({_pct(sizes.get('tp2'))}) · "
        f"runner {_pct(sizes.get('runner'))} trail {_pct(sizes.get('trailing_stop_pct'))}",
    ]
    return "\n".join(lines)


def _component_line(components: dict) -> str:
    parts = [f"{label} {int(round(components.get(key, 0)))}"
             for key, label in _COMPONENT_LABELS if key in components]
    return " · ".join(parts) if parts else "—"


def _metrics_line(m: dict) -> str:
    parts = []
    if m.get("atr_ratio") is not None:
        parts.append(f"ATR14/60 {m['atr_ratio']:.2f}")
    if m.get("volume_ratio") is not None:
        parts.append(f"vol {m['volume_ratio']:.1f}x")
    if m.get("range_position") is not None:
        parts.append(f"range pos {m['range_position'] * 100:.0f}%")
    if m.get("fdv_mc") is not None:
        parts.append(f"FDV/MC {m['fdv_mc']:.1f}x")
    return " · ".join(parts)


def _footer(flagged: list) -> str:
    lines = []
    if flagged:
        tags = ", ".join(
            f"${_esc(str(f.get('symbol', '?')))} ({_esc(_first_flag(f))})"
            for f in flagged[:5]
        )
        lines.append(f"⚠️ <b>FLAGGED</b> (di-exclude): {tags}")
    lines.append("<i>PAPER MODE — signal dicatat buat evaluasi expectancy, "
                 "bukan sinyal beli. NFA — DYOR</i>")
    return "\n".join(lines)


def _first_flag(f: dict) -> str:
    flags = f.get("flags") or []
    return str(flags[0]) if flags else "flagged"


def _pct(v) -> str:
    if v is None:
        return "?"
    return f"{v * 100:.0f}%"


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)
