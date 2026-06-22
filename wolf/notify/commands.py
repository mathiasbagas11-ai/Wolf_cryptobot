"""Telegram command router.

Maps an incoming ``/command`` text to a reply string, using the live
:class:`~wolf.app.Application` components. Pure text-in / text-out so it is
trivially unit-testable; the network side (long-polling) lives in
:mod:`wolf.notify.poller`.

Commands:
    /analyze <SYM>  technical read on one coin (default if a bare ticker is sent)
    /stats          aggregate win-rate / PnL
    /paper          paper-trading balance, R, drawdown
    /learning       per-strategy edge + blacklist
    /active         open (pending/active) signals
    /help           this list
"""

from __future__ import annotations

import logging

from wolf.textfmt import DIVIDER, esc

log = logging.getLogger("wolf.commands")

_HELP = (
    "🐺 <b>Wolf — Commands</b>\n" + DIVIDER + "\n"
    "<code>/analyze BTC</code> — analyse a coin\n"
    "<code>/stats</code> — win-rate &amp; PnL\n"
    "<code>/paper</code> — paper balance &amp; R\n"
    "<code>/learning</code> — strategy edge &amp; blacklist\n"
    "<code>/active</code> — open signals\n"
    "<code>/help</code> — this message"
)


class CommandRouter:
    def __init__(self, app) -> None:
        self._app = app

    def handle(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        parts = text.split()
        cmd = parts[0].lstrip("/").lower()
        # Strip @botname suffix Telegram adds in groups (e.g. /stats@WolfBot).
        cmd = cmd.split("@", 1)[0]
        arg = " ".join(parts[1:]).strip()

        if cmd in ("start", "help"):
            return _HELP
        if cmd == "analyze":
            if self._app.analyze is None:
                return "⚠️ Analysis unavailable."
            return self._app.analyze.analyze(arg)
        if cmd == "stats":
            return self._stats()
        if cmd == "paper":
            return self._paper()
        if cmd == "learning":
            return self._learning()
        if cmd == "active":
            return self._active()
        # Bare ticker shortcut: "/btc" -> analyse BTC (only when no extra args).
        if self._app.analyze is not None and not arg and cmd.isalnum():
            return self._app.analyze.analyze(cmd)
        return "❓ Unknown command. Try <code>/help</code>."

    # ── builders ─────────────────────────────────────────────────────────
    def _stats(self) -> str:
        s = self._app.tracker.stats()
        return (
            f"📊 <b>STATS</b>\n{DIVIDER}\n"
            f"✅ {s['wins']} / 🛑 {s['losses']} · WR {s['win_rate']}%\n"
            f"💰 Avg PnL {s['avg_pnl_pct']:+.2f}% · 🔵 Active {s['active']} · Graded {s['total_graded']}"
        )

    def _paper(self) -> str:
        if self._app.paper is None:
            return "Paper trading is disabled."
        p = self._app.paper.stats()
        return (
            f"🏦 <b>PAPER ACCOUNT</b>\n{DIVIDER}\n"
            f"Balance <b>{p['balance']:,.2f}</b> USD ({p['return_pct']:+.2f}%)\n"
            f"Peak {p['peak']:,.2f} · Max DD {p['max_drawdown_pct']:.2f}%\n"
            f"Trades {p['trades']} · Total {p['total_r']:+.2f}R · Avg {p['avg_r']:+.2f}R"
        )

    def _learning(self) -> str:
        if self._app.learning is None:
            return "Learning is disabled."
        snap = self._app.learning.snapshot()
        lines = [f"🧠 <b>LEARNING</b>\n{DIVIDER}"]
        strat = snap.get("strategies", {})
        if not strat:
            lines.append("No history yet.")
        for name, b in sorted(strat.items(), key=lambda kv: -kv[1]["win_rate"]):
            lines.append(f"• {esc(name)} {b['win_rate']:.0f}% ({b['trades']} trades, {b['avg_r']:+.2f}R)")
        if snap.get("blacklist"):
            lines.append(f"⛔ Blacklist: {esc(', '.join(snap['blacklist']))}")
        return "\n".join(lines)

    def _active(self) -> str:
        signals = self._app.tracker.active_signals()
        if not signals:
            return "No open signals."
        lines = [f"🔵 <b>OPEN SIGNALS ({len(signals)})</b>\n{DIVIDER}"]
        for s in signals[:20]:
            lines.append(f"• {esc(s.symbol)} {esc(s.direction)} · {esc(s.strategy)} · {esc(s.status)}")
        return "\n".join(lines)
