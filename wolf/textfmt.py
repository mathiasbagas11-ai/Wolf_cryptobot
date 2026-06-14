"""Shared text formatting helpers for Telegram cards.

Small, pure functions used by the notifier and the report builders so price /
escaping / divider formatting is defined once.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DIVIDER = "━━━━━━━━━━━━━━━━━━"


def fmt_price(p) -> str:
    """Format a price with sensible precision across BTC-scale and sub-cent."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "?"
    if p == 0:
        return "0"
    ap = abs(p)
    if ap >= 1000:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:,.4f}"
    if ap >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def fmt_usd(v) -> str:
    """Compact USD for large notional values (e.g. 1.2M, 850K)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    av = abs(v)
    if av >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if av >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if av >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def now(tz_name: str = "UTC") -> str:
    """Current time formatted in ``tz_name`` with its abbreviation.

    Falls back to UTC if the zone name is invalid, so a bad ``TIMEZONE`` env
    never crashes a notification.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    dt = datetime.now(tz)
    label = dt.strftime("%Z") or "UTC"
    return f"{dt.strftime('%Y-%m-%d %H:%M')} {label}"
