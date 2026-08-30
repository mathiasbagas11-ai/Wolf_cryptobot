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

from wolf.analyze import normalize_symbol
from wolf.risk_plan import build_plan, render_plan
from wolf.textfmt import DIVIDER, esc, fmt_price

log = logging.getLogger("wolf.commands")

_HELP = (
    "🐺 <b>Wolf — Commands</b>\n" + DIVIDER + "\n"
    "<code>/analyze BTC</code> — analyse a coin\n"
    "<code>/calc BTC 500</code> — position size &amp; leverage for your balance\n"
    "<code>/stats</code> — win-rate &amp; PnL\n"
    "<code>/paper</code> — paper balance &amp; return\n"
    "<code>/learning</code> — strategy edge &amp; blacklist\n"
    "<code>/active</code> — open signals\n"
    "<code>/diag</code> — diagnostic digest now (add hours, e.g. <code>/diag 48</code>)\n"
    "<code>/whatif</code> — re-grade past signals under each stop rule\n"
    "<code>/whatif cost</code> — what each max_cost_r would have kept\n"
    "<code>/ai</code> — is the debate layer actually answering?\n"
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
        if cmd == "calc":
            return self._calc(arg)
        if cmd == "stats":
            return self._stats()
        if cmd == "paper":
            return self._paper()
        if cmd == "learning":
            return self._learning()
        if cmd == "active":
            return self._active()
        if cmd == "whatif":
            return self._whatif(arg)
        if cmd == "ai":
            return self._ai()
        if cmd == "diag":
            return self._diag(arg)
        if getattr(self._app, "analyze", None) is not None and not arg and cmd.isalnum():
            return self._app.analyze.analyze(cmd)  # bare ticker shortcut
        return "❓ Unknown command. Try <code>/help</code>."

    def _ai(self) -> str:
        """Ask the arbiter for one verdict and report what came back.

        Whether the layer works is otherwise only visible in the next day's
        digest, because a broken one abstains rather than failing — so a fix
        applied now cannot be confirmed until the abstentions it caused have
        aged out of the window. One live call answers it immediately.
        """
        from wolf.app import ai_status

        try:
            st = ai_status(self._app, probe=True)
        except Exception:
            log.exception("AI probe failed")
            return "⚠️ AI probe failed — see logs."
        if not st.get("enabled"):
            return "🤖 <b>AI debate</b>: OFF (<code>AI_DEBATE_ENABLED</code> is false)"
        if st.get("available"):
            degraded = ", ".join(st.get("degraded_roles") or [])
            extra = f"\nWeak roles: {esc(degraded)}" if degraded else ""
            return f"🤖 <b>AI debate</b>: OK — the arbiter returned a verdict.{extra}"
        return (
            "🤖 <b>AI debate</b>: ENABLED but not answering\n"
            f"<code>{esc(st.get('reason') or 'unknown')}</code>"
        )

    def _whatif(self, arg: str) -> str:
        """Re-score resolved signals under a rule that was not the one in force.

        ``/whatif cost`` prices the max_cost_r gate, which needs only the stop
        distance already on each record. Bare ``/whatif`` compares the
        stop-advance rules, which has to refetch the candles behind every
        signal — slow, one request each, hence a command and not part of the
        daily card.
        """
        from wolf.whatif import (
            compare_cost_gates, compare_stop_rules, render, render_cost_gates,
        )

        try:
            if arg.lower().startswith("cost"):
                report = compare_cost_gates(
                    self._app.tracker,
                    round_trip_bps=self._app.settings.round_trip_cost_bps,
                )
                return f"<pre>{esc(render_cost_gates(report))}</pre>"
            report = compare_stop_rules(self._app.tracker)
        except Exception:
            log.exception("What-if comparison failed")
            return "⚠️ What-if failed — see logs."
        return f"<pre>{esc(render(report))}</pre>"

    def _diag(self, arg: str) -> str:
        """The digest the daily report sends, on demand."""
        from wolf.app import ai_status
        from wolf.diagnose import diagnose, render_digest

        try:
            hours = float(arg) if arg else 24.0
        except ValueError:
            return "⚠️ Usage: <code>/diag</code> or <code>/diag 48</code>"
        try:
            digest = render_digest(diagnose(
                self._app.tracker,
                window_hours=hours,
                round_trip_bps=self._app.settings.round_trip_cost_bps,
                tp1_banks_win=self._app.settings.tracker.tp1_banks_win,
                state_dir=self._app.settings.state_dir,
                ai_available=ai_status(self._app)["available"],
            ))
        except Exception:
            log.exception("Diagnostics digest failed")
            return "⚠️ Diagnostics failed — see logs."
        return f"<pre>{esc(digest)}</pre>"

    def _calc(self, arg: str) -> str:
        parts = arg.split()
        if not parts:
            return "⚠️ Usage: <code>/calc BTC 500</code> (coin + your balance in USD)"
        analyze = getattr(self._app, "analyze", None)
        if analyze is None:
            return "⚠️ Sizing unavailable."
        symbol = normalize_symbol(parts[0])
        acct = getattr(self._app, "account", None)
        default_bal = acct.balance if acct is not None else 1000.0
        try:
            balance = float(parts[1]) if len(parts) > 1 else default_bal
        except ValueError:
            return "⚠️ Balance must be a number, e.g. <code>/calc BTC 500</code>"
        if balance <= 0:
            return "⚠️ Balance must be positive."

        cand = analyze.latest_setup(symbol)
        if cand is None:
            return f"➖ No active setup on <b>{esc(symbol)}</b> to size right now."
        risk = self._app.settings.risk
        plan = build_plan(
            cand.entry_price, cand.sl, cand.direction == "LONG", balance,
            self._app.settings.paper_risk_pct,
            max_leverage=risk.max_leverage,
            mmr=risk.maintenance_margin_rate,
            buffer=risk.liq_safety_buffer,
        )
        if plan is None:
            return f"⚠️ Could not size <b>{esc(symbol)}</b> (bad geometry)."
        emoji = "🟢" if cand.direction == "LONG" else "🔴"
        head = (
            f"{emoji} <b>{esc(symbol)} {esc(cand.direction)}</b> · {esc(cand.strategy)}\n"
            f"Entry <code>{fmt_price(cand.entry_price)}</code> · "
            f"SL <code>{fmt_price(cand.sl)}</code> · TP <code>{fmt_price(cand.tp)}</code>\n{DIVIDER}\n"
        )
        return head + render_plan(plan, balance, fmt_price)

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
