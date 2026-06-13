"""Telegram notifier.

Sends formatted lifecycle notifications to Telegram. Message routing to forum
topics (threads) is configured via :class:`~wolf.config.TelegramSettings`. When
no token/chat is configured the notifier becomes a no-op (handy for tests and
local runs) instead of raising.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from wolf.config import TelegramSettings
from wolf.models import Signal, Status

log = logging.getLogger("wolf.telegram")


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
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("Telegram send failed: %s", exc)
            return False

    # ── tracker callback ────────────────────────────────────────────────
    def on_event(self, signal: Signal, event: str, info: dict) -> None:
        """Adapter matching :data:`wolf.tracker.NotifyFn`."""
        if event == "ACTIVATED":
            text = self._activated_text(signal)
            self.send(text, self._settings.market_update_thread_id)
        elif event == "TP_HIT":
            text = self._tp_text(signal, info)
            self.send(text, self._settings.market_update_thread_id)
        elif event == "RESOLVED":
            text = self._resolved_text(signal)
            self.send(text, self._settings.trade_report_thread_id)

    def announce_signal(self, signal: Signal) -> None:
        self.send(self._new_signal_text(signal), self._settings.new_signal_thread_id)

    # ── message builders ────────────────────────────────────────────────
    @staticmethod
    def _emoji(direction: str) -> str:
        return "🟢" if direction.upper() == "LONG" else "🔴"

    def _new_signal_text(self, s: Signal) -> str:
        ladder = "\n".join(
            f"   TP{r['level']}: <code>{r['price']:.6g}</code>" for r in s.tp_ladder
        ) or f"   TP: <code>{s.tp:.6g}</code>"
        reasons = "\n".join(f"   • {r}" for r in s.reasons)
        return (
            f"{self._emoji(s.direction)} <b>NEW {s.signal_type}</b> — {s.symbol} {s.direction}\n"
            f"Strategy: {s.strategy} | Score: {s.score} ({s.confluence_level})\n"
            f"Entry: <code>{s.entry_price:.6g}</code>\n{ladder}\n"
            f"   SL: <code>{s.sl:.6g}</code>\n"
            f"{reasons}"
        )

    def _activated_text(self, s: Signal) -> str:
        return (
            f"⚡ <b>ACTIVATED</b> — {s.symbol} {s.direction}\n"
            f"Entry zone touched @ <code>{s.entry_price:.6g}</code>"
        )

    def _tp_text(self, s: Signal, info: dict) -> str:
        lvl = info.get("level", "?")
        price = info.get("price")
        price_str = f"{price:.6g}" if isinstance(price, (int, float)) else "?"
        return (
            f"✅ <b>TP{lvl} HIT</b> — {s.symbol} {s.direction} @ <code>{price_str}</code>\n"
            f"Stop moved to breakeven."
        )

    def _resolved_text(self, s: Signal) -> str:
        status = Status(s.status)
        icon = "🎯" if status.is_win else ("🛑" if status.is_loss else "⏳")
        pnl = s.pnl_pct if s.pnl_pct is not None else 0.0
        return (
            f"{icon} <b>{s.status}</b> — {s.symbol} {s.direction}\n"
            f"PnL: <b>{pnl:+.2f}%</b> | Hold: {s.hold_hours or 0:.1f}h\n"
            f"Strategy: {s.strategy} | Score: {s.score}"
        )
