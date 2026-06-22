"""Telegram command router.

Maps an incoming ``/command`` text to a reply string using the live
:class:`~wolf.app.Application` components. Pure text-in / text-out so it is
trivially unit-testable; the network side (long-polling) lives in
:mod:`wolf.notify.poller`.

Commands:
    /analyze <SYM>  technical read on one coin (bare /btc works too)
    /stats          aggregate win-rate / PnL
    /paper          paper-trading balance, return, drawdown
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
    "<code>/paper</code> — paper balance &amp; return\n"
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
        cmd = parts[0].lstrip("/").lower().split("@", 1)[0]  # strip @botname suffix
        arg = " ".join(parts[1:]).strip()

        if cmd in ("start", "help"):
            return _HELP
        if cmd == "analyze":
            if getattr(self._app, "analyze", None) is None:
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
        if getattr(self._app, "analyze", None) is not None and not arg and cmd.isalnum():
            return self._app.analyze.analyze(cmd)  # bare ticker shortcut
        return "❓ Unknown command. Try <code>/help</code>."

    # ── builders ─────────────────────────────────────────────────────────
    def _stats(self) -> str:
        s = self._app.tracker.stats()
        return (
            f"📊 <b>STATS</b>\n{DIVIDER}\n"
            f"✅ {s.get('wins', 0)} / 🛑 {s.get('losses', 0)} · WR {s.get('win_rate', 0)}%\n"
            f"💰 Avg PnL {s.get('avg_pnl_pct', 0):+.2f}% · 🔵 Active {s.get('active', 0)} "
            f"· Graded {s.get('total_graded', 0)}"
        )

    def _paper(self) -> str:
        acct = getattr(self._app, "account", None)
        if acct is None:
            return "Paper trading is disabled."
        p = acct.summary()
        return (
            f"🏦 <b>PAPER ACCOUNT</b>\n{DIVIDER}\n"
            f"Balance <b>{p['balance']:,.2f}</b> USD ({p['return_pct']:+.2f}%)\n"
            f"Peak {p['peak']:,.2f} · Max DD {p['max_drawdown_pct']:.2f}%\n"
            f"Trades {p['trades']} · Realized {p['realized']:+.2f} USD"
        )

    def _learning(self) -> str:
        learning = getattr(self._app, "learning", None)
        if learning is None:
            return "Learning is disabled."
        snap = learning.snapshot()
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
