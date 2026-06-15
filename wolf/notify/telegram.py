"""Telegram notifier.

Sends formatted notifications to Telegram, routed to forum topics (threads) via
:class:`~wolf.config.TelegramSettings`. Design goals:

* **Per-topic routing with fallback** — each message type goes to its own topic,
  falling back to the main channel when that topic isn't configured, so nothing
  is silently dropped.
* **Loud failures** — Telegram API errors are logged with their *description*
  (e.g. "message thread not found", "chat not found"), which is what you need to
  diagnose a misconfigured chat/topic.
* **Safe content** — dynamic text is HTML-escaped.
* **Local time** — timestamps render in the configured timezone (default WIB).
* **No-op when unconfigured** — without a token/chat the notifier does nothing.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from wolf.config import TelegramSettings
from wolf.models import Signal, Status
from wolf.textfmt import DIVIDER, esc, fmt_price, now

log = logging.getLogger("wolf.telegram")


def _pct(price: float, entry: float, is_long: bool) -> float:
    if not entry:
        return 0.0
    return (price - entry) / entry * 100 if is_long else (entry - price) / entry * 100


class TelegramNotifier:
    def __init__(
        self,
        settings: TelegramSettings,
        timeout: float = 10.0,
        tz: str = "UTC",
        session: Optional[requests.Session] = None,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._tz = tz
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def _stamp(self) -> str:
        return f"🕐 {now(self._tz)}"

    # ── transport ───────────────────────────────────────────────────────
    def send(self, text: str, thread_id: str = "") -> bool:
        ok, desc, _mid = self._post(text, thread_id)
        # If the topic is misconfigured (wrong/stale thread id), don't drop the
        # message — retry once on the main channel so the alert still lands.
        if not ok and thread_id and self._is_bad_thread(desc):
            log.warning(
                "thread=%s invalid (%s) — falling back to main channel", thread_id, desc
            )
            ok, _desc, _mid = self._post(text, "")
        return ok

    @staticmethod
    def _is_bad_thread(description: str) -> bool:
        d = (description or "").lower()
        return "thread" in d or "topic" in d

    def _post(self, text: str, thread_id: str = "") -> tuple[bool, str, Optional[int]]:
        """Low-level send. Returns ``(ok, error_description, message_id)``."""
        if not self.enabled:
            log.debug("Telegram disabled; dropping message")
            return False, "disabled", None
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
            return False, str(exc), None
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
            return False, description, None
        message_id = None
        try:
            message_id = resp.json().get("result", {}).get("message_id")
        except ValueError:
            message_id = None
        return True, "", message_id

    def _delete(self, message_id: int) -> None:
        """Best-effort delete of a probe message; failures are non-fatal."""
        url = f"https://api.telegram.org/bot{self._settings.bot_token}/deleteMessage"
        try:
            self._session.post(
                url,
                json={"chat_id": self._settings.chat_id, "message_id": message_id},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            log.debug("Probe delete failed for %s: %s", message_id, exc)

    def validate_threads(self) -> dict:
        """Probe every configured topic and report which thread ids are invalid.

        Sends a tiny probe message to each routed topic (deleting it again on
        success) so a wrong or stale ``*_THREAD_ID`` is surfaced once at startup
        — with a clear label — instead of failing silently on every later post.
        Returns ``{"ok": [...], "bad": [(label, tid, reason)]}``.
        """
        result: dict = {"ok": [], "bad": []}
        if not self.enabled:
            return result
        for label, tid in self._settings.configured_threads():
            ok, desc, mid = self._post(f"🔎 thread check: {esc(label)}", tid)
            if ok:
                result["ok"].append((label, tid))
                if mid is not None:
                    self._delete(mid)
            else:
                result["bad"].append((label, tid, desc or "send failed"))
        return result

    def report_thread_validation(self, result: dict) -> None:
        """Log a summary and, if any topic is misconfigured, post it to General."""
        bad = result.get("bad", [])
        ok = result.get("ok", [])
        if not bad:
            log.info("Telegram topics OK: %d configured topic(s) reachable", len(ok))
            return
        for label, tid, reason in bad:
            log.warning("Telegram topic INVALID: %s (thread=%s) — %s", label, tid, reason)
        lines = [
            f"⚠️ <b>TOPIC CHECK</b>\n{DIVIDER}",
            f"{len(ok)} OK · {len(bad)} misconfigured:",
        ]
        for label, tid, reason in bad:
            lines.append(f"• <b>{esc(label)}</b> (id <code>{esc(str(tid))}</code>) — {esc(reason)}")
        lines.append(
            "Fix the matching <code>*_THREAD_ID</code> env var (or blank it to use "
            "the main channel)."
        )
        lines.append(self._stamp())
        # Post to General if it's valid, else fall back to the main channel.
        bad_ids = {tid for _, tid, _ in bad}
        sys_route = self._settings.route_system()
        thread = "" if sys_route in bad_ids else sys_route
        self.send("\n".join(lines), thread)

    # ── lifecycle notifications ─────────────────────────────────────────
    def notify_startup(self, info: dict) -> None:
        sources = " → ".join(info.get("sources", [])) or "—"
        detectors = ", ".join(info.get("detectors", [])) or "—"
        text = (
            f"🐺 <b>Wolf Crypto Tracker — ONLINE</b>\n{DIVIDER}\n"
            f"📡 Sources: {esc(sources)}\n"
            f"🎯 Detectors: {esc(detectors)}\n"
            f"🪙 Universe: {info.get('universe', 0)} pairs\n"
            f"⏱ Scan every {info.get('scan_min', '?')}m · Track every {info.get('track_min', '?')}m\n"
            f"🧠 AI debate: {info.get('ai_mode', 'OFF')}\n"
            f"{self._stamp()}"
        )
        self.send(text, self._settings.route_system())

    def announce_signal(self, signal: Signal) -> None:
        self.send(self._signal_card(signal), self._settings.route_new_signal())

    def on_event(self, signal: Signal, event: str, info: dict) -> None:
        """Adapter matching :data:`wolf.tracker.NotifyFn`."""
        if event == "ACTIVATED":
            self.send(self._activated_text(signal), self._settings.route_entry())
        elif event == "TP_HIT":
            self.send(self._tp_text(signal, info), self._settings.route_entry())
        elif event == "RESOLVED":
            self.send(self._resolved_text(signal, info), self._settings.route_trade_report())

    def notify_stats(self, stats: dict) -> None:
        self.send(self._stats_card(stats), self._settings.route_stats())

    def notify_news(self, items) -> None:
        if items:
            self.send(self._news_card(items), self._settings.route_news())

    # ── market report notifications (text built by the reporters) ───────
    def notify_majors(self, text: str) -> None:
        if text:
            self.send(text, self._settings.route_majors())

    def notify_radar(self, text: str) -> None:
        if text:
            self.send(text, self._settings.route_radar())

    def notify_pulse(self, text: str) -> None:
        if text:
            self.send(text, self._settings.route_market_update())

    def notify_whale(self, text: str) -> None:
        if text:
            self.send(text, self._settings.route_whale())

    # ── message builders ────────────────────────────────────────────────
    @staticmethod
    def _dir_emoji(direction: str) -> str:
        return "🟢" if direction.upper() == "LONG" else "🔴"

    def _ai_block(self, s: Signal) -> str:
        """Return a formatted AI verdict line, or empty string if no AI ran."""
        if not s.ai_verdict or s.ai_verdict == "ABSTAIN":
            return ""
        if s.ai_vetoed:
            label = f"⚠️ REJECT ({s.ai_confidence}%) — sent anyway (monitor)"
        elif s.ai_verdict == "CONFIRM":
            label = f"✅ CONFIRM ({s.ai_confidence}%)"
        else:
            label = f"⚖️ {esc(s.ai_verdict)} ({s.ai_confidence}%)"
        rationale = f" — {esc(s.ai_rationale)}" if s.ai_rationale else ""
        return f"🧠 AI: {label}{rationale}\n"

    def _signal_card(self, s: Signal) -> str:
        is_long = s.is_long
        ladder = s.tp_ladder or [{"level": 1, "price": s.tp}]
        tp_lines = [
            f"🎯 TP{r['level']}  <code>{fmt_price(r['price'])}</code>  "
            f"({_pct(r['price'], s.entry_price, is_long):+.2f}%)"
            for r in ladder
        ]
        sl_pct = _pct(s.sl, s.entry_price, is_long)
        risk = abs(s.entry_price - s.sl)
        reward = abs(ladder[-1]["price"] - s.entry_price)
        rr = reward / risk if risk else 0.0
        reasons = "\n".join(f"• {esc(r)}" for r in s.reasons) or "• —"
        return (
            f"{self._dir_emoji(s.direction)} <b>NEW SIGNAL · {esc(s.signal_type)}</b>\n"
            f"<b>{esc(s.symbol)}</b> · {esc(s.direction)}\n{DIVIDER}\n"
            f"💵 Entry  <code>{fmt_price(s.entry_price)}</code>\n"
            + "\n".join(tp_lines) + "\n"
            f"🛑 SL     <code>{fmt_price(s.sl)}</code>  ({sl_pct:+.2f}%)\n"
            f"📊 Score {s.score}/100 · {esc(s.confluence_level or '—')} · R:R {rr:.1f}\n"
            f"⚡ {esc(s.strategy)} · {esc(s.entry_mode)}\n{DIVIDER}\n"
            f"{self._ai_block(s)}"
            f"{reasons}\n{self._stamp()}"
        )

    def _activated_text(self, s: Signal) -> str:
        return (
            f"⚡ <b>ENTRY TOUCHED</b> · {esc(s.symbol)} {esc(s.direction)}\n"
            f"Now active @ <code>{fmt_price(s.entry_price)}</code> — monitoring TP/SL\n"
            f"{self._stamp()}"
        )

    def _tp_text(self, s: Signal, info: dict) -> str:
        lvl = info.get("level", "?")
        price = info.get("price")
        pct = _pct(price, s.entry_price, s.is_long) if isinstance(price, (int, float)) else 0.0
        return (
            f"✅ <b>TP{lvl} HIT</b> · {esc(s.symbol)} {esc(s.direction)}\n"
            f"Price <code>{fmt_price(price)}</code> ({pct:+.2f}%) — stop moved to breakeven\n"
            f"{self._stamp()}"
        )

    def _resolved_text(self, s: Signal, info: Optional[dict] = None) -> str:
        info = info or {}
        status = Status(s.status)
        head = "🎯 <b>WIN" if status.is_win else ("🛑 <b>LOSS" if status.is_loss else "⚪ <b>CLOSED")
        pnl = s.pnl_pct if s.pnl_pct is not None else 0.0
        hold = s.hold_hours if s.hold_hours is not None else 0.0
        tp_final = (s.tp_ladder[-1]["price"] if s.tp_ladder else s.tp)
        exit_str = fmt_price(s.exit_price) if s.exit_price is not None else "—"

        lines = [
            f"{head} · {esc(s.status)}</b> · {esc(s.symbol)} {esc(s.direction)}",
            f"💵 Entry <code>{fmt_price(s.entry_price)}</code> → Exit <code>{exit_str}</code>",
            f"🎯 TP <code>{fmt_price(tp_final)}</code> · 🛑 SL <code>{fmt_price(s.sl)}</code>",
        ]
        # PnL line — add currency move + R multiple when the paper account ran.
        pnl_line = f"📈 PnL <b>{pnl:+.2f}%</b>"
        if "r_multiple" in info:
            pnl_line += f" · {info['r_multiple']:+.2f}R"
        if "pnl_amount" in info:
            pnl_line += f" · {info['pnl_amount']:+.2f} USD"
        pnl_line += f" · held {hold:.1f}h · {esc(s.strategy)}"
        lines.append(pnl_line)
        if "balance" in info:
            lines.append(f"🏦 Paper balance <b>{info['balance']:.2f} USD</b>")
        if info.get("lesson"):
            lines.append(f"🧠 <i>{esc(info['lesson'])}</i>")
        lines.append(self._stamp())
        return "\n".join(lines)

    def _news_card(self, items) -> str:
        lines = [f"📰 <b>CRYPTO NEWS</b>\n{DIVIDER}"]
        for it in items:
            title = esc(it.title)
            src = f" — <i>{esc(it.source)}</i>" if it.source else ""
            if it.url:
                lines.append(f"• <a href=\"{esc(it.url)}\">{title}</a>{src}")
            else:
                lines.append(f"• {title}{src}")
        lines.append(self._stamp())
        return "\n".join(lines)

    def _stats_card(self, stats: dict) -> str:
        lines = [
            f"📊 <b>PERFORMANCE SUMMARY</b>\n{DIVIDER}",
            f"✅ Wins {stats.get('wins', 0)} · 🛑 Losses {stats.get('losses', 0)} "
            f"· 📈 Win rate {stats.get('win_rate', 0)}%",
            f"💰 Avg PnL {stats.get('avg_pnl_pct', 0):+.2f}% · 🔵 Active {stats.get('active', 0)}",
        ]

        by_strategy = stats.get("by_strategy", {})
        if by_strategy:
            lines.append("\n<b>By strategy</b>")
            for name, b in sorted(by_strategy.items()):
                lines.append(
                    f"• {esc(name)}  {b.get('win_rate', 0)}% "
                    f"({b.get('total', 0)} trades, {b.get('avg_pnl', 0):+.2f}%)"
                )

        by_ai = stats.get("by_ai_verdict", {})
        has_ai_data = any(k not in ("NO_AI", "") for k in by_ai)
        if has_ai_data:
            lines.append("\n<b>AI verdict accuracy</b>")
            verdict_order = ["CONFIRM", "NEUTRAL", "REJECT", "ABSTAIN", "NO_AI"]
            ordered = sorted(by_ai.items(), key=lambda kv: verdict_order.index(kv[0]) if kv[0] in verdict_order else 99)
            for verdict, b in ordered:
                emoji = {"CONFIRM": "✅", "NEUTRAL": "⚖️", "REJECT": "⚠️", "ABSTAIN": "🔇", "NO_AI": "—"}.get(verdict, "•")
                lines.append(
                    f"{emoji} {esc(verdict)}  {b.get('win_rate', 0)}% "
                    f"({b.get('total', 0)} trades, {b.get('avg_pnl', 0):+.2f}%)"
                )
            # Veto readiness signal: if AI-flagged REJECT signals lose significantly
            # more often than average, enabling veto mode is justified.
            vetoed_wr = stats.get("vetoed_win_rate")
            vetoed_n = stats.get("vetoed_count", 0)
            overall_wr = stats.get("win_rate", 0)
            if vetoed_wr is not None and vetoed_n > 0:
                delta = vetoed_wr - overall_wr
                readiness = "🔴 consider veto mode" if delta <= -15 else ("🟡 monitor more" if delta <= 0 else "🟢 AI over-cautious")
                lines.append(
                    f"🛡 Vetoed signals: {vetoed_wr}% win ({vetoed_n} total, {delta:+.0f}% vs avg) — {readiness}"
                )

        lines.append(f"\n{self._stamp()}")
        return "\n".join(lines)
