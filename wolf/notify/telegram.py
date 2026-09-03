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

from wolf.config import LadderSettings, TelegramSettings
from wolf.models import Signal, Status
from wolf.risk_plan import build_plan, render_plan
from wolf.textfmt import DIVIDER, esc, fmt_price, now

log = logging.getLogger("wolf.telegram")

# Signal types routed to the dedicated High-Conviction topic (when configured).
HIGH_CONVICTION_TYPES = frozenset({"TRAP"})


#: How long a setup on each interval is meant to be held. Stated on the card
#: because the same "1:3" reads completely differently on 15m and 4h: the
#: targets are ATR multiples of that series, so a 4h signal is a multi-day
#: position while a 15m one is done within the session.
_HORIZON = {
    "15m": "intraday",
    "30m": "intraday",
    "1h": "1-2 days",
    "4h": "swing, days",
    "1d": "position, weeks",
}


def _horizon(s: Signal) -> str:
    tf = (s.timeframe or "15m").lower()
    label = _HORIZON.get(tf)
    return f"{tf} · {label}" if label else tf


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
        risk=None,
        account=None,
        risk_pct: float = 2.0,
        start_balance: float = 1000.0,
        ladder=None,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._tz = tz
        self._session = session or requests.Session()
        #: Thread ids already reported as unusable, so a rejected topic is
        #: announced once rather than on every message it misroutes.
        self._announced_fallbacks: set[str] = set()
        self._risk = risk
        self._account = account
        self._risk_pct = risk_pct
        self._start_balance = start_balance
        self._ladder = ladder or LadderSettings()

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
            self._announce_fallback(thread_id, desc)
            ok, _desc, _mid = self._post(text, "")
        return ok

    def _announce_fallback(self, thread_id: str, desc: str) -> None:
        """Say once, in the channel it is landing in, why it is landing there.

        The retry keeps a message from being lost, which is right, and makes
        the misrouting invisible, which is not: a recurring digest whose topic
        the provider rejects simply becomes the main channel's feed, and the
        only account of it is a log line. Every reading of "why is this here"
        then has to be a guess.

        Once per thread id per process, matching what the startup probe does
        for a configured topic — reported once instead of on every message.
        The startup probe cannot cover this case anyway: a topic can be closed,
        deleted, or the bot removed from it long after boot.
        """
        if thread_id in self._announced_fallbacks:
            return
        self._announced_fallbacks.add(thread_id)
        self._post(
            f"⚠️ <b>Topic unavailable</b> — thread <code>{esc(thread_id)}</code> "
            f"rejected: <code>{esc(desc or 'unknown')}</code>\n"
            f"Messages routed there are landing here until it is fixed.",
            "",
        )

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

    def send_raw(self, chat_id: str, text: str, thread_id: str = "") -> bool:
        """Send to an explicit chat/thread (used to reply to incoming commands)."""
        if not self._settings.bot_token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{self._settings.bot_token}/sendMessage"
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            log.warning("Telegram reply error: %s", exc)
            return False
        if resp.status_code != 200:
            log.warning("Telegram reply failed (%s)", resp.status_code)
            return False
        return True

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
        # Named only when something actually lands here: a line that always
        # prints is a line nobody reads, and on a fully routed deployment
        # there is nothing to say.
        unrouted = (
            f"📬 Into this channel: {esc(str(info['unrouted']))}\n"
            if info.get("unrouted") else ""
        )
        text = (
            f"🐺 <b>Wolf Crypto Tracker — ONLINE</b>\n{DIVIDER}\n"
            f"📡 Sources: {esc(sources)}\n"
            f"🎯 Detectors: {esc(detectors)}\n"
            f"🪙 Universe: {info.get('universe', 0)} pairs\n"
            f"⏱ Scan every {info.get('scan_min', '?')}m · Track every {info.get('track_min', '?')}m\n"
            f"🛡 Risk gates: {info.get('risk_gates', '—')}\n"
            f"🧠 AI debate: {esc(str(info.get('ai_mode', 'OFF')))}\n"
            f"💾 State: {esc(str(info.get('state', '—')))}\n"
            f"{unrouted}"
            f"{self._stamp()}"
        )
        self.send(text, self._settings.route_system())

    def _route_signal(self, signal: Signal, default_thread: str) -> str:
        """Divert high-conviction signal types to their own topic.

        Returns the High-Conviction thread when the signal is a premium type
        *and* that topic is configured; otherwise the message stays on its
        normal per-event route, so behaviour is unchanged when the topic is unset.
        """
        if signal.signal_type in HIGH_CONVICTION_TYPES:
            hc = self._settings.route_high_conviction()
            if hc:
                return hc
        return default_thread

    def announce_signal(self, signal: Signal) -> None:
        self.send(self._signal_card(signal), self._route_signal(signal, self._settings.route_new_signal()))

    def on_event(self, signal: Signal, event: str, info: dict) -> None:
        """Adapter matching :data:`wolf.tracker.NotifyFn`."""
        if event == "ACTIVATED":
            self.send(self._activated_text(signal), self._route_signal(signal, self._settings.route_entry()))
        elif event == "TP_HIT":
            self.send(self._tp_text(signal, info), self._route_signal(signal, self._settings.route_entry()))
        elif event == "RESOLVED":
            self.send(self._resolved_text(signal, info), self._route_signal(signal, self._settings.route_trade_report()))

    def notify_stats(self, stats: dict, all_time: Optional[dict] = None) -> None:
        self.send(self._stats_card(stats, all_time), self._settings.route_stats())

    def notify_diagnostics(self, digest: str) -> None:
        """Post the diagnostic digest as a preformatted block.

        Sent as a second message rather than folded into the card: the card is
        for reading at a glance, this is for copying whole into an analysis.
        """
        if not digest:
            return
        self.send(f"<pre>{esc(digest)}</pre>", self._settings.route_stats())

    def notify_news(self, items) -> None:
        if items:
            self.send(self._news_card(items), self._settings.route_news())

    def notify_news_digest(self, text: str) -> None:
        """Post an AI-synthesised news brief (already plain text) to the News topic."""
        if text:
            body = f"📰 <b>CRYPTO NEWS</b>\n{DIVIDER}\n{esc(text)}\n{self._stamp()}"
            self.send(body, self._settings.route_news())

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

    def notify_flow(self, text: str) -> None:
        if text:
            self.send(text, self._settings.route_flow())

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

    def _risk_block(self, s: Signal) -> str:
        """Return a formatted risk-flag line, or empty string if unflagged."""
        flags = []
        if s.against_regime:
            flags.append("against-regime")
        if s.weak_strategy:
            flags.append("weak-strategy")
        if not flags:
            return ""
        return f"🛡 Risk: {esc(' · '.join(flags))} (monitor)\n"

    def _breakeven_note(self, stats: dict) -> str:
        """Show the win rate actually needed, next to the one achieved.

        Derived from realised wins and losses when there are any, and only from
        the configured geometry as a cold-start fallback. The two answers are
        far apart: the ladder's ceiling assumes every winner runs to the last
        rung, while a front-loaded scale-out plus a breakeven stop means the
        typical winner banks about 0.5R. Quoting the ceiling (~37%) against a
        reality that needs ~65% is the kind of comfortable arithmetic that makes
        a losing system look nearly fine.
        """
        win_rate = stats.get("win_rate", 0)
        needed = stats.get("breakeven_win_rate") or 0.0
        source = "realised"
        if not needed:
            needed = self._ladder.breakeven_win_rate
            source = "geometry"
        mark = "✅" if win_rate >= needed else "⚠️"
        return f" ({mark} vs {needed:.0f}% needed, {source})"

    def _signal_card(self, s: Signal) -> str:
        is_long = s.is_long
        ladder = s.tp_ladder or [{"level": 1, "price": s.tp}]
        # Each rung shows its R multiple and the size closed there, so the card
        # states what the trade returns rather than only the best price it may
        # touch — R:R alone describes the last rung, which rarely fills.
        tp_lines = []
        for r in ladder:
            extra = []
            if r.get("r_multiple"):
                extra.append(f"{r['r_multiple']:.1f}R")
            if r.get("allocation"):
                extra.append(f"close {r['allocation'] * 100:.0f}%")
            suffix = f"  · {' · '.join(extra)}" if extra else ""
            tp_lines.append(
                f"🎯 TP{r['level']}  <code>{fmt_price(r['price'])}</code>  "
                f"({_pct(r['price'], s.entry_price, is_long):+.2f}%){suffix}"
            )
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
            f"⚡ {esc(s.strategy)} · {esc(s.entry_mode)} · {esc(_horizon(s))}\n{DIVIDER}\n"
            f"{self._plan_block(s)}"
            f"{self._risk_block(s)}"
            f"{self._ai_block(s)}"
            f"{reasons}\n{self._stamp()}"
        )

    def _plan_block(self, s: Signal) -> str:
        """Executable position-sizing + liquidation plan, or '' if disabled."""
        if not (self._risk and getattr(self._risk, "plan_enabled", False)):
            return ""
        balance = self._account.balance if self._account is not None else self._start_balance
        # Bounce guard shrinks the risked fraction for flagged shorts (1.0 = full).
        risk_pct = self._risk_pct * (getattr(s, "risk_scale", 1.0) or 1.0)
        plan = build_plan(
            s.entry_price, s.sl, s.is_long, balance, risk_pct,
            max_leverage=self._risk.max_leverage,
            mmr=self._risk.maintenance_margin_rate,
            buffer=self._risk.liq_safety_buffer,
        )
        if plan is None:
            return ""
        return render_plan(plan, balance, fmt_price) + f"{DIVIDER}\n"

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

    def _stats_card(self, stats: dict, all_time: Optional[dict] = None) -> str:
        window = stats.get("window_hours")
        title = f"PERFORMANCE SUMMARY · {window:g}h" if window else "PERFORMANCE SUMMARY"
        flat_n = stats.get("flat", 0)
        flat_tag = f" · 😐 Flat {flat_n}" if flat_n else ""
        lines = [
            f"📊 <b>{esc(title)}</b>\n{DIVIDER}",
            f"✅ Wins {stats.get('wins', 0)} · 🛑 Losses {stats.get('losses', 0)} "
            f"· 📈 Win rate {stats.get('win_rate', 0)}%"
            + self._breakeven_note(stats)
            + ("" if stats.get("conclusive", True) else "  ⚠️ small sample"),
            # Expectancy leads in R: targets are ATR multiples, so a percentage
            # average is dominated by whichever volatile symbols happened to trade.
            f"💰 Expectancy {stats.get('avg_r', 0):+.2f}R "
            f"({stats.get('avg_pnl_pct', 0):+.2f}%){flat_tag} "
            f"· 🔵 Active {stats.get('active', 0)}",
        ]
        if all_time:
            lines.append(
                f"📚 All-time {all_time.get('win_rate', 0)}% WR · "
                f"{all_time.get('avg_r', 0):+.2f}R over {all_time.get('total_traded', 0)}"
            )

        by_strategy = stats.get("by_strategy", {})
        if by_strategy:
            lines.append("\n<b>By strategy</b>")
            for name, b in sorted(by_strategy.items()):
                fired = b.get("emitted", b.get("total", 0))
                graded_n = b.get("total", 0)
                active_n = b.get("active", 0)
                active_tag = f" · {active_n} active" if active_n else ""
                mark = "" if b.get("conclusive", True) else " ⚠️"
                lines.append(
                    f"• {esc(name)}  {b.get('win_rate', 0)}% WR · {b.get('avg_r', 0):+.2f}R "
                    f"({graded_n}/{fired} graded{active_tag}, avg {b.get('avg_pnl', 0):+.2f}%){mark}"
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

        # Risk-gate monitor: compare flagged signals' win-rate to overall, so a
        # monitored gate can be promoted to a hard block once it's proven.
        overall_wr = stats.get("win_rate", 0)
        gate_lines = []
        for label, n_key, wr_key, hard_hint in (
            ("Against-regime", "against_regime_count", "against_regime_win_rate", "REGIME_HARD_BLOCK"),
            ("Weak-strategy", "weak_flag_count", "weak_flag_win_rate", "AUTOPAUSE_HARD_BLOCK"),
        ):
            wr = stats.get(wr_key)
            n = stats.get(n_key, 0)
            if wr is not None and n > 0:
                delta = wr - overall_wr
                hint = f"🔴 enable {hard_hint}" if delta <= -15 else ("🟡 keep monitoring" if delta <= 0 else "🟢 not hurting")
                gate_lines.append(f"• {label}: {wr}% win ({n} total, {delta:+.0f}% vs avg) — {hint}")
        if gate_lines:
            lines.append("\n<b>Risk-gate monitor</b>")
            lines += gate_lines

        lines.append(f"\n{self._stamp()}")
        return "\n".join(lines)
