"""Telegram notifier.

Sends formatted lifecycle notifications to Telegram, routed to forum topics
(threads) via :class:`~wolf.config.TelegramSettings`. Design goals:

* **Per-topic routing with fallback** — each message type prefers its own topic
  but falls back to a more general one (and ultimately the main channel), so
  nothing is silently dropped when only some topics are configured.
* **Loud failures** — on a Telegram API error the response *description* is
  logged (e.g. "chat not found", "message thread not found"), which is what you
  need to diagnose a misconfigured chat/topic.
* **Safe content** — dynamic text (a signal's reasons) is HTML-escaped before
  being placed in an HTML-parsed message.
* **No-op when unconfigured** — without a token/chat the notifier does nothing
  instead of raising, so the bot runs fine with Telegram off.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from wolf.config import TelegramSettings
from wolf.models import Signal, Status

log = logging.getLogger("wolf.telegram")

_DIVIDER = "━━━━━━━━━━━━━━━━━━"


def _fmt_price(p) -> str:
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


def _pct(price: float, entry: float, is_long: bool) -> float:
    if not entry:
        return 0.0
    return (price - entry) / entry * 100 if is_long else (entry - price) / entry * 100


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


class TelegramNotifier:
    def __init__(
        self,
        settings: TelegramSettings,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    # ── transport ───────────────────────────────────────────────────────
    def send(self, text: str, thread_id: str = "") -> bool:
        if not self.enabled:
            log.debug("Telegram disabled; dropping message")
            return False
        url = f"https://api.telegram.org/bot{self._settings.bot_token}/sendMessage"
        payload: dict = {
            "chat_id": self._settings.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            log.warning("Telegram send error: %s", exc)
            return False
        if resp.status_code != 200:
            description = ""
            try:
                description = resp.json().get("description", "")
            except ValueError:
                description = resp.text[:200]
            log.warning(
                "Telegram send failed (%s) thread=%s: %s",
                resp.status_code, thread_id or "main", description,
            )
            return False
        return True

    # ── high-level notifications ────────────────────────────────────────
    def notify_startup(self, info: dict) -> None:
        sources = " → ".join(info.get("sources", [])) or "—"
        detectors = ", ".join(info.get("detectors", [])) or "—"
        text = (
            f"🐺 <b>Wolf Crypto Tracker — ONLINE</b>\n{_DIVIDER}\n"
            f"📡 Sources: {_esc(sources)}\n"
            f"🎯 Detectors: {_esc(detectors)}\n"
            f"🪙 Universe: {info.get('universe', 0)} pairs\n"
            f"⏱ Scan every {info.get('scan_min', '?')}m · Track every {info.get('track_min', '?')}m\n"
            f"🧠 AI debate: {'ON' if info.get('ai') else 'OFF'}\n"
            f"🕐 {self._now()} UTC"
        )
        self.send(text, self._settings.route_system())

    def announce_signal(self, signal: Signal) -> None:
        self.send(self._signal_card(signal), self._settings.route_new_signal())

    def notify_stats(self, stats: dict) -> None:
        self.send(self._stats_card(stats), self._settings.route_stats())

    def on_event(self, signal: Signal, event: str, info: dict) -> None:
        """Adapter matching :data:`wolf.tracker.NotifyFn`."""
        if event == "ACTIVATED":
            self.send(self._activated_text(signal), self._settings.route_market_update())
        elif event == "TP_HIT":
            self.send(self._tp_text(signal, info), self._settings.route_market_update())
        elif event == "RESOLVED":
            self.send(self._resolved_text(signal), self._settings.route_trade_report())

    # ── message builders ────────────────────────────────────────────────
    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _dir_emoji(direction: str) -> str:
        return "🟢" if direction.upper() == "LONG" else "🔴"

    def _signal_card(self, s: Signal) -> str:
        is_long = s.is_long
        ladder = s.tp_ladder or [{"level": 1, "price": s.tp}]
        tp_lines = []
        for r in ladder:
            price = r["price"]
            tp_lines.append(
                f"🎯 TP{r['level']}  <code>{_fmt_price(price)}</code>  "
                f"({_pct(price, s.entry_price, is_long):+.2f}%)"
            )
        sl_pct = _pct(s.sl, s.entry_price, is_long)
        risk = abs(s.entry_price - s.sl)
        reward = abs(ladder[-1]["price"] - s.entry_price)
        rr = reward / risk if risk else 0.0
        reasons = "\n".join(f"• {_esc(r)}" for r in s.reasons) or "• —"
        return (
            f"{self._dir_emoji(s.direction)} <b>NEW SIGNAL · {_esc(s.signal_type)}</b>\n"
            f"<b>{_esc(s.symbol)}</b> · {_esc(s.direction)}\n{_DIVIDER}\n"
            f"💵 Entry  <code>{_fmt_price(s.entry_price)}</code>\n"
            + "\n".join(tp_lines) + "\n"
            f"🛑 SL     <code>{_fmt_price(s.sl)}</code>  ({sl_pct:+.2f}%)\n"
            f"📊 Score {s.score}/100 · {_esc(s.confluence_level or '—')} · R:R {rr:.1f}\n"
            f"⚡ {_esc(s.strategy)} · {_esc(s.entry_mode)}\n{_DIVIDER}\n"
            f"{reasons}"
        )

    def _activated_text(self, s: Signal) -> str:
        return (
            f"⚡ <b>ENTRY TOUCHED</b> · {_esc(s.symbol)} {_esc(s.direction)}\n"
            f"Now active @ <code>{_fmt_price(s.entry_price)}</code> — monitoring TP/SL"
        )

    def _tp_text(self, s: Signal, info: dict) -> str:
        lvl = info.get("level", "?")
        price = info.get("price")
        pct = _pct(price, s.entry_price, s.is_long) if isinstance(price, (int, float)) else 0.0
        return (
            f"✅ <b>TP{lvl} HIT</b> · {_esc(s.symbol)} {_esc(s.direction)}\n"
            f"Price <code>{_fmt_price(price)}</code> ({pct:+.2f}%) — stop moved to breakeven"
        )

    def _resolved_text(self, s: Signal) -> str:
        status = Status(s.status)
        if status.is_win:
            head = "🎯 <b>WIN"
        elif status.is_loss:
            head = "🛑 <b>LOSS"
        else:
            head = "⚪ <b>CLOSED"
        pnl = s.pnl_pct if s.pnl_pct is not None else 0.0
        hold = s.hold_hours if s.hold_hours is not None else 0.0
        return (
            f"{head} · {_esc(s.status)}</b> · {_esc(s.symbol)} {_esc(s.direction)}\n"
            f"PnL <b>{pnl:+.2f}%</b> · held {hold:.1f}h · {_esc(s.strategy)}"
        )

    def _stats_card(self, stats: dict) -> str:
        lines = [
            f"📊 <b>PERFORMANCE SUMMARY</b>\n{_DIVIDER}",
            f"✅ Wins {stats.get('wins', 0)} · 🛑 Losses {stats.get('losses', 0)} "
            f"· 📈 Win rate {stats.get('win_rate', 0)}%",
            f"💰 Avg PnL {stats.get('avg_pnl_pct', 0):+.2f}% · 🔵 Active {stats.get('active', 0)}",
        ]
        by_strategy = stats.get("by_strategy", {})
        if by_strategy:
            lines.append("\n<b>By strategy</b>")
            for name, b in by_strategy.items():
                lines.append(
                    f"• {_esc(name)}  {b.get('win_rate', 0)}% "
                    f"({b.get('total', 0)} trades, {b.get('avg_pnl', 0):+.2f}%)"
                )
        lines.append(f"\n🕐 {self._now()} UTC")
        return "\n".join(lines)
