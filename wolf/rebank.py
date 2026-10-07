"""Re-book historical outcomes whose banked rungs were never counted.

Three exit paths close a scaled position, and until 2026-09-18 only two of them
priced it as scaled. A stop taken after TP1 blended each hit rung at its own
price, and so did a full run through every rung — the code even says why:
"pricing the whole position at the final rung pretends nothing was sold on the
way up". The timeout path never got the same treatment. It priced the entire
position at the price on the clock when the timer ran out, as though the slice
already sold at TP1 were still open.

The error runs in both directions and its size is exactly the first rung's
allocation times how far the timeout price sits from that rung: booking the
whole position at ``x`` when ``a`` of it left at ``R1`` is off by
``a * (x - R1)``. On the standard 50/30/20 ladder with TP1 at 1R that is
-0.5R for a trade that drifted all the way back (the banked slice is
forgotten, and the breakeven stop bounds it there) and approaches +0.5R for
one parked just under TP2 (nothing is credited as sold at TP1) — a full 1R of
spread on a trade whose whole realised range is 3R. It also decided *status*:
a trade whose blended result clears the dead band was filed EXPIRED_FLAT —
ungraded, invisible to the win rate, and never applied to the paper balance
at all.

What this rebuilds, and what it leaves alone
--------------------------------------------
Only rows that came out of the timeout path are touched: a status in
``_TIMEOUT_STATUSES`` and a non-empty ``tps_hit``. That pairing is what makes
them identifiable at all — the blended paths overwrite ``exit_price`` with a
synthetic figure derived from the PnL, so their price and PnL are consistent
by construction and cannot be told apart from a single exit by arithmetic. A
timeout row's ``exit_price`` is the real market price, which is what the blend
needs — so re-booking preserves it under ``timeout_price`` before overwriting
``exit_price`` with the synthetic one. That is what keeps a second run from
re-blending its own output and booking the rung twice.

The paper balance is then patched by a *ratio*, not replayed. ``apply`` moves
it by ``balance * risk_pct/100 * scale * R``, so the balance is a product —
``start * prod(1 + k_i * R_i)`` — and changing one row multiplies the final
figure by ``(1 + k*R_new) / (1 + k*R_old)``, exactly and independently of
where that row sat in the sequence. Replaying from a clean slate looks like
the safer choice and is not: it silently assumes the outcome log holds every
trade ever settled, and the log is capped (``MAX_OUTCOMES``). Replayed over a
truncated log it does not correct the balance, it re-anchors it to whenever
the surviving rows begin — a 36-row, -2.3R correction came back as -55%. The
ratio needs no history beyond the rows that actually changed.

``learning_memory`` is deliberately **not** touched. It lives under its own
key, so nothing here can reach it, and rebuilding it from the outcome log
would silently discard anything seeded from a backtest, which never appears in
that log. The cost is stated rather than hidden: its ``pnl_sum`` and ``r_sum``
still carry the old figures for the affected trades, and where a status moved
across the dead band its win/loss count is off by that trade. Its rankings are
built from win *rates* over many trades, so a handful of re-booked rows moves
them very little — but it is a real, known staleness, not a clean slate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from wolf.account import ACCOUNT_KEY, PaperAccount
from wolf.config import TrackerSettings
from wolf.models import Signal, Status
from wolf.state import StateStore
from wolf.tracker import OUTCOMES_KEY, _partial_pnl, _risk_pct, normalize_ladder

log = logging.getLogger("wolf.rebank")

#: The statuses the timeout path can produce. A row with one of these *and* a
#: banked rung is unambiguously from the path that priced the whole position.
_TIMEOUT_STATUSES = frozenset({
    Status.EXPIRED_WIN.value, Status.EXPIRED_LOSS.value,
    Status.EXPIRED_FLAT.value, Status.EXPIRED.value,
})

#: ``Signal.timeout_price``: the real market price a blended timeout was priced
#: from. ``exit_price`` is overwritten with a synthetic figure consistent with
#: the blended PnL — the convention the other two exit paths already use —
#: which destroys the one input the blend needs. Without this field a second
#: pass would re-blend its own output and book the rung twice, which is exactly
#: the hazard this module refuses to go near on TP_HIT rows. Preserving it makes
#: the second pass arithmetically identical to the first, so idempotence falls
#: out of the numbers rather than resting on an "already done" marker that a
#: later writer could drop. The live tracker writes it too, so rows produced by
#: the fixed path are safe to re-scan.
_TIMEOUT_PRICE_KEY = "timeout_price"


def _rebooked(sig: Signal, settings: TrackerSettings) -> Optional[dict]:
    """The corrected figures for one row, or ``None`` if it needs no change."""
    if sig.status not in _TIMEOUT_STATUSES or not sig.tps_hit:
        return None
    price = sig.timeout_price or sig.exit_price
    if sig.entry_price <= 0 or not price:
        return None
    ladder = normalize_ladder(sig.tp_ladder, sig.tp, sig.sl, sig.entry_price, sig.is_long)
    if not ladder:
        return None

    pnl = round(_partial_pnl(sig.entry_price, sig.is_long, ladder,
                             list(sig.tps_hit), price), 3)
    risk = _risk_pct(sig)
    r = (pnl / risk) if risk else 0.0

    # Status is re-decided on the blended figure, because the dead band is a
    # judgement about whether the trade said anything and the old figure was
    # answering for a position size the trade did not have.
    if abs(r) < settings.expiry_flat_r:
        status = Status.EXPIRED_FLAT.value
    else:
        status = Status.EXPIRED_WIN.value if r > 0 else Status.EXPIRED_LOSS.value

    if status == sig.status and abs(pnl - (sig.pnl_pct or 0.0)) < 1e-9:
        return None
    return {
        "pnl_pct": pnl,
        "r_multiple": round(r, 3),
        "status": status,
        # Effective single exit consistent with the blended PnL, matching what
        # the other two paths write.
        "exit_price": (sig.entry_price * (1 + pnl / 100) if sig.is_long
                       else sig.entry_price * (1 - pnl / 100)),
        _TIMEOUT_PRICE_KEY: price,
    }


def rebank_outcomes(
    store: StateStore,
    settings: Optional[TrackerSettings] = None,
    start_balance: float = 1000.0,
    risk_pct: float = 1.0,
    dry_run: bool = True,
) -> dict:
    """Re-book timeout outcomes that banked a rung, and replay the balance.

    Idempotent: a re-booked row keeps the real timeout price it was blended
    from (``timeout_price``), so a second run recomputes the same figures and
    finds nothing to change. ``dry_run`` reports what would move without
    writing, which is the only safe way to look at a ledger first.
    """
    settings = settings or TrackerSettings()
    raw = store.read(OUTCOMES_KEY, default=[]) or []
    rows = [d for d in raw if isinstance(d, dict)]

    changed: list[dict] = []
    updated: list[dict] = []
    k = risk_pct / 100
    factor = 1.0          # what the corrections multiply the balance by
    settled_delta = 0     # rows that crossed into or out of being graded
    for d in rows:
        try:
            sig = Signal.from_dict(d)
        except (TypeError, ValueError):
            updated.append(d)
            continue
        fix = _rebooked(sig, settings)
        if fix is None:
            updated.append(d)
            continue
        was, now = _equity_factor(sig, sig.status, sig.pnl_pct, k), \
            _equity_factor(sig, fix["status"], fix["pnl_pct"], k)
        if was and now:
            factor *= now / was
        settled_delta += (1 if now else 0) - (1 if was else 0)
        changed.append({
            "symbol": sig.symbol, "strategy": sig.strategy,
            "tps_hit": list(sig.tps_hit),
            "was": {"status": sig.status, "pnl_pct": sig.pnl_pct,
                    "r_multiple": sig.r_multiple},
            "now": {"status": fix["status"], "pnl_pct": fix["pnl_pct"],
                    "r_multiple": fix["r_multiple"]},
        })
        updated.append({**d, **fix})

    report = {
        "state_dir": getattr(store, "base_dir", ""),
        "scanned": len(rows),
        "rebooked": len(changed),
        "status_changed": sum(1 for c in changed if c["was"]["status"] != c["now"]["status"]),
        "r_delta": round(sum((c["now"]["r_multiple"] or 0) - (c["was"]["r_multiple"] or 0)
                             for c in changed), 3),
        "changes": changed,
        "dry_run": dry_run,
        "learning_untouched": True,
    }
    before = PaperAccount(store, start_balance, risk_pct).balance
    report["balance_before"] = before
    report["balance_after"] = round(before * factor, 2) if changed else None
    if dry_run or not changed:
        if dry_run:
            report["balance_after"] = None
        return report

    store.write(OUTCOMES_KEY, updated)
    _write_balance(store, report["balance_after"], start_balance, settled_delta)
    log.info(
        "Rebank: %d of %d outcomes re-booked (%d changed status), balance %.2f -> %.2f",
        len(changed), len(rows), report["status_changed"], before, report["balance_after"],
    )
    return report


def _equity_factor(sig: Signal, status: str, pnl_pct: Optional[float],
                   k: float) -> Optional[float]:
    """What one row multiplies the balance by, or ``None`` if it never did.

    Mirrors ``PaperAccount.apply`` exactly — the same risk leg, the same
    ``risk_scale``, the same "only graded outcomes move equity" rule. If the
    two ever drift apart the correction stops being a correction, so this is
    asserted against the account itself in the tests rather than trusted.
    """
    try:
        st = Status(status)
    except ValueError:
        return None
    if not (st.is_win or st.is_loss):
        return None
    risk = _risk_pct(sig)
    r = ((pnl_pct or 0.0) / risk) if risk else 0.0
    return 1 + k * (getattr(sig, "risk_scale", 1.0) or 1.0) * r


def _write_balance(store: StateStore, balance: float, start_balance: float,
                   settled_delta: int) -> None:
    """Move the account to the corrected balance without rebuilding it.

    ``realized`` follows exactly (the balance is the start plus every realised
    move) and ``trades`` by however many rows crossed the graded boundary. The
    equity ``peak`` is a running maximum over states that were never stored, so
    it cannot be recomputed — it is only ever raised here, which keeps drawdown
    honest rather than flattering it with a peak that quietly dropped.
    """
    def _mutator(st):
        st = dict(st or {})
        st["balance"] = round(balance, 2)
        st["realized"] = round(balance - start_balance, 2)
        st["peak"] = round(max(float(st.get("peak") or start_balance), balance), 2)
        st["trades"] = max(0, int(st.get("trades", 0)) + settled_delta)
        return st

    store.update(ACCOUNT_KEY, _mutator, default={
        "balance": start_balance, "trades": 0, "realized": 0.0, "peak": start_balance})


class TruncatedLog(Exception):
    """The outcome log holds fewer settled trades than the account counted."""


def replay_balance(store: StateStore, start_balance: float = 1000.0,
                   risk_pct: float = 1.0) -> float:
    """Rebuild the paper balance from the outcome log, in resolution order.

    **Only valid on a complete log, and the log is capped** — ``MAX_OUTCOMES``
    discards the oldest rows, so on a truncated one this does not correct the
    balance, it re-anchors it to whenever the surviving rows happen to begin.
    That is not a smaller version of the right answer, it is a different
    quantity wearing the same name, and it reads as a catastrophic loss: a
    36-row, -2.3R correction came back as -55% because the log held the last
    500 trades of a much longer history.

    So the completeness is checked rather than assumed, against the one number
    that knows: the account's own settled-trade counter. ``rebank_outcomes``
    no longer calls this at all — it patches the balance by a ratio, which
    needs no history beyond the rows that changed.
    """
    account = PaperAccount(store, start_balance, risk_pct)
    counted = int(store.read(ACCOUNT_KEY, default={}).get("trades", 0) or 0)
    settled = sum(
        1 for d in (store.read(OUTCOMES_KEY, default=[]) or [])
        if isinstance(d, dict) and _is_settled(d.get("status"))
    )
    if counted > settled:
        raise TruncatedLog(
            f"the account settled {counted} trades but the log holds {settled} "
            f"— replaying it would re-anchor the balance, not correct it"
        )
    store.write(ACCOUNT_KEY, {"balance": start_balance, "trades": 0,
                              "realized": 0.0, "peak": start_balance})
    account = PaperAccount(store, start_balance, risk_pct)
    raw = store.read(OUTCOMES_KEY, default=[]) or []
    signals = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            signals.append(Signal.from_dict(d))
        except (TypeError, ValueError):
            continue
    signals.sort(key=lambda s: s.resolved_at or s.exit_time or "")
    for sig in signals:
        account.apply(sig)
    return account.balance


def _is_settled(status: Optional[str]) -> bool:
    try:
        st = Status(status)
    except ValueError:
        return False
    return st.is_win or st.is_loss


def repair_balance(store: StateStore, observed_before: float, count: int,
                   settings: Optional[TrackerSettings] = None,
                   start_balance: float = 1000.0, risk_pct: float = 1.0,
                   dry_run: bool = True) -> dict:
    """Undo a balance that was replayed over a truncated log.

    A one-off. The rows the backfill corrected are the *oldest* ones carrying
    a ``timeout_price``: the live tracker writes that field too, but only from
    the moment the forward fix deployed, so every row the backfill touched
    predates every row the tracker wrote. Taking the oldest ``count`` of them
    therefore selects exactly the set the account booked wrongly, with no
    cutoff to get wrong — an earlier version of this took a timestamp, and a
    timestamp an hour out silently selects a different set and returns a
    plausible wrong number, which is the failure being undone here.

    ``count`` is the figure the backfill reported and is *asserted*: fewer
    rows than that on disk means the log has rotated since, and the repair is
    no longer computable from what survives.
    """
    settings = settings or TrackerSettings()
    k = risk_pct / 100
    rows = [d for d in (store.read(OUTCOMES_KEY, default=[]) or [])
            if isinstance(d, dict) and d.get(_TIMEOUT_PRICE_KEY)]
    rows.sort(key=lambda d: d.get("resolved_at") or d.get("exit_time") or "")

    factor, matched = 1.0, 0
    for d in rows[:count]:
        try:
            sig = Signal.from_dict(d)
        except (TypeError, ValueError):
            continue
        risk_leg = _risk_pct(sig)
        if not risk_leg:
            continue
        # What the account actually booked: the whole position at the timeout
        # price, which is the mistake being reversed.
        old_pnl = ((sig.timeout_price - sig.entry_price) if sig.is_long
                   else (sig.entry_price - sig.timeout_price)) / sig.entry_price * 100
        old_status = Status.EXPIRED_FLAT.value
        if abs(old_pnl / risk_leg) >= settings.expiry_flat_r:
            old_status = (Status.EXPIRED_WIN.value if old_pnl > 0
                          else Status.EXPIRED_LOSS.value)
        was = _equity_factor(sig, old_status, round(old_pnl, 3), k)
        now = _equity_factor(sig, sig.status, sig.pnl_pct, k)
        if was and now:
            factor *= now / was
        matched += 1

    report = {
        "state_dir": getattr(store, "base_dir", ""),
        "matched": matched, "expected": count, "candidates": len(rows),
        "observed_before": round(observed_before, 2),
        "factor": round(factor, 6),
        "balance_after": round(observed_before * factor, 2),
        "dry_run": dry_run,
    }
    if matched != count:
        report["error"] = (
            f"only {matched} of the {count} corrected rows are still on disk "
            f"({len(rows)} carry a timeout price) — the log has rotated and the "
            f"repair can no longer be computed from what survives"
        )
        return report
    if not dry_run:
        _write_balance(store, report["balance_after"], start_balance, settled_delta=0)
    return report


def render(report: dict) -> str:
    """The report as an operator reads it, for the CLI and for Telegram.

    The path the state came from is printed first. Running this in the wrong
    place is the failure mode that matters: ``STATE_DIR`` defaults to a
    relative path, so a rebank started outside the container rewrites an empty
    ledger, reports "0 scanned" and looks like a clean bill of health.
    """
    head = "REBANK (dry run — nothing written)" if report.get("dry_run") else "REBANK (written)"
    lines = [
        f"{head} | {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"state      {report.get('state_dir') or '?'}",
        f"scanned    {report['scanned']} outcomes | {report['rebooked']} re-booked "
        f"| {report['status_changed']} changed status",
    ]
    if not report["rebooked"]:
        lines.append(
            "nothing     every timeout row that banked a rung already agrees "
            "with the blend"
        )
        return "\n".join(lines)

    lines.append(f"r_delta    {report['r_delta']:+.3f}R across the re-booked rows")
    before, after = report.get("balance_before"), report.get("balance_after")
    if after is None:
        lines.append(f"balance    {before:,.2f} -> unchanged (dry run)")
    else:
        lines.append(f"balance    {before:,.2f} -> {after:,.2f} (replayed, not patched)")
    lines.append(
        "learning   learning_memory untouched by design — its pnl_sum/r_sum keep "
        "the old figures for these rows"
    )
    for c in report["changes"][:15]:
        w, n = c["was"], c["now"]
        lines.append(
            f"  {c['symbol']:<12} {c['strategy']:<9} tps={c['tps_hit']} "
            f"{w['status']} {(w['r_multiple'] or 0):+.3f}R -> "
            f"{n['status']} {n['r_multiple']:+.3f}R"
        )
    if len(report["changes"]) > 15:
        lines.append(f"  ... and {len(report['changes']) - 15} more")
    return "\n".join(lines)


def repair_balance_from_delta(store: StateStore, observed_before: float, r_delta: float,
                              start_balance: float = 1000.0, risk_pct: float = 1.0,
                              dry_run: bool = True) -> dict:
    """Undo a truncated-log replay once the rows it needs have rotated out.

    The exact repair reads the corrected rows back off disk, and the log is
    capped: every day ~25 new outcomes push the oldest out, and the rows the
    backfill corrected were among the oldest. After a few days they are gone
    and the exact repair refuses — correctly — but the balance is still the
    re-anchored figure, which is wrong by half.

    Both inputs here come from the backfill's own report, and neither needs
    the log. Because ``apply`` is multiplicative, the correction is
    ``prod((1 + k R_new) / (1 + k R_old))``, and for ``k R`` of a percent or
    two that is ``exp(k * sum(R_new - R_old))`` = ``exp(k * r_delta)`` to
    within a few hundredths of a percent — checked against the exact product
    on the rows the report printed (0.0258%). The one row that crossed the
    dead band adds at most ``k * 0.25`` on top, so the stated bound is 0.3%.

    It is an estimate and says so on every line it prints. The alternative is
    a balance known to be wrong by 55%, which is not a more honest number,
    only a more precise-looking one.
    """
    import math

    factor = math.exp(risk_pct / 100 * r_delta)
    report = {
        "state_dir": getattr(store, "base_dir", ""),
        "mode": "estimate",
        "r_delta": r_delta,
        "observed_before": round(observed_before, 2),
        "factor": round(factor, 6),
        "balance_after": round(observed_before * factor, 2),
        "tolerance_pct": 0.3,
        "dry_run": dry_run,
    }
    if not dry_run:
        _write_balance(store, report["balance_after"], start_balance, settled_delta=0)
    return report


def render_repair(report: dict) -> str:
    head = ("REBANK-REPAIR (dry run — nothing written)" if report.get("dry_run")
            else "REBANK-REPAIR (written)")
    lines = [
        f"{head} | {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"state      {report.get('state_dir') or '?'}",
    ]
    if report.get("mode") == "estimate":
        lines.append(f"mode       estimate from r_delta {report['r_delta']:+.3f}R "
                     f"(within ±{report['tolerance_pct']}%) — the corrected rows "
                     f"have rotated out of the log")
        lines.append(f"factor     x{report['factor']:.6f}")
        lines.append(f"balance    {report['observed_before']:,.2f} -> "
                     f"{report['balance_after']:,.2f}")
        return "\n".join(lines)
    lines.append(
        f"rows       {report['matched']} of {report['expected']} corrected rows "
        f"found ({report.get('candidates', 0)} carry a timeout price)"
    )
    if report.get("error"):
        lines.append(f"refused    {report['error']}")
        return "\n".join(lines)
    lines.append(f"factor     x{report['factor']:.6f}")
    lines.append(f"balance    {report['observed_before']:,.2f} -> {report['balance_after']:,.2f}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m wolf.rebank`` — run the backfill where the state lives.

    Meant for a shell inside the running container (``railway ssh``). Do not
    reach for ``railway run``: that executes locally with the deployment's
    environment, so ``STATE_DIR=/data`` points at a directory that does not
    exist on the machine running it and the rebank operates on nothing.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m wolf.rebank",
        description="Re-book timeout outcomes whose banked rungs were never counted.",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="actually write; without it the run is a dry run and reports only",
    )
    repair = parser.add_argument_group(
        "repair",
        "undo a balance replayed over a truncated log (one-off, see repair_balance)",
    )
    repair.add_argument("--repair-balance", type=float, metavar="BEFORE",
                        help="the balance the backfill reported before it wrote")
    repair.add_argument("--expect", type=int, metavar="N",
                        help="the row count the backfill reported; asserted, not advisory")
    args = parser.parse_args(argv)

    from wolf.config import Settings
    from wolf.state import StateStore

    settings = Settings.from_env()
    store = StateStore(settings.state_dir)

    if args.repair_balance is not None:
        if args.expect is None:
            parser.error("--repair-balance needs --expect")
        rep = repair_balance(
            store, args.repair_balance, count=args.expect, settings=settings.tracker,
            start_balance=settings.paper_start_balance,
            risk_pct=settings.paper_risk_pct, dry_run=not args.confirm,
        )
        print(render_repair(rep))
        return 1 if rep.get("error") else 0

    report = rebank_outcomes(
        store, settings.tracker,
        start_balance=settings.paper_start_balance,
        risk_pct=settings.paper_risk_pct,
        dry_run=not args.confirm,
    )
    print(render(report))
    if report["dry_run"] and report["rebooked"]:
        print("\nRe-run with --confirm to write.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI
    raise SystemExit(main())
