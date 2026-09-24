"""Whale-coordination alert → 👁 Whale Report topic.

Event-driven, and that is the whole distinction from the Flow Intelligence
digest. This fires the moment several tracked wallets pile into the same coin in
the same direction; the digest reports, on a timer, where everyone is sitting.
Different rhythm, different reason to open the room — which is why they get
different topics.

Pure formatting only. The collector decides *what* is an event (and holds the
per-coin cooldown that stops a build-up re-alerting every scan); this decides
how it reads. Nothing here fetches or persists.
"""

from __future__ import annotations

import logging
from typing import Optional

from wolf.textfmt import DIVIDER, esc, fmt_usd, now

log = logging.getLogger("wolf.reports")

#: Wallet addresses shown before the list is truncated.
MAX_WALLETS_SHOWN = 5


def short_address(address: str) -> str:
    """``0x1234…abcd`` — enough to recognise a wallet, short enough to scan."""
    text = str(address or "")
    return f"{text[:6]}…{text[-4:]}" if len(text) >= 12 else text


def format_coordination_alert(
    coin: str,
    direction: str,
    wallet_count: int,
    notional_usd: float,
    wallets: Optional[list] = None,
    *,
    tz: str = "UTC",
) -> str:
    """Render one coordinated-entry alert.

    Deliberately states only what was observed — who moved, which way, how much.
    No entry, no target, no stop: this room reports positioning, and what to do
    about it is the detectors' call.
    """
    side = str(direction).upper()
    emoji = "🟢" if side == "LONG" else "🔴"
    label = "AKUMULASI" if side == "LONG" else "DISTRIBUSI"

    lines = [
        f"🐳 <b>WHALE COORDINATION</b>\n{DIVIDER}",
        f"{emoji} <b>${esc(coin)}</b> — {esc(label)} / {esc(side)}",
        f"👥 <b>{int(wallet_count)} wallet</b> buka/tambah posisi bersamaan",
        f"💰 Total notional {fmt_usd(notional_usd)}",
    ]

    for i, wallet in enumerate((wallets or [])[:MAX_WALLETS_SHOWN], 1):
        if not isinstance(wallet, dict):
            continue
        mark = "🆕" if wallet.get("is_new") else "➕"
        lines.append(
            f"  {i}. {mark} <code>{esc(short_address(wallet.get('addr', '')))}</code> "
            f"{fmt_usd(wallet.get('notional'))}"
        )

    lines.append(f"{DIVIDER}\n🕐 {now(tz)}")
    return "\n".join(lines)


def build_coordination_alerts(doc: dict, *, tz: str = "UTC") -> list[str]:
    """Render every coordinated entry in a collector snapshot, largest first.

    ``doc["coins"]`` already carries only events that cleared the wallet
    threshold and are outside their cooldown, so everything here is worth
    sending. An empty list is the normal case — most scans see no coordination.
    """
    coins = doc.get("coins") if isinstance(doc, dict) else None
    if not isinstance(coins, dict) or not coins:
        return []

    ordered = sorted(
        coins.items(),
        key=lambda kv: _f(kv[1].get("notional_usd")) if isinstance(kv[1], dict) else 0.0,
        reverse=True,
    )
    alerts: list[str] = []
    for coin, row in ordered:
        if not isinstance(row, dict):
            continue
        alerts.append(format_coordination_alert(
            coin,
            row.get("direction", ""),
            _f(row.get("wallet_count")),
            _f(row.get("notional_usd")),
            row.get("wallets"),
            tz=tz,
        ))
    return alerts


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
