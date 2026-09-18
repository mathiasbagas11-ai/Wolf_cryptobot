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

The paper balance is then replayed from a clean slate over the corrected log,
because it is a running compound of ``balance * risk_pct`` and cannot be
patched in place.

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
        "scanned": len(rows),
        "rebooked": len(changed),
        "status_changed": sum(1 for c in changed if c["was"]["status"] != c["now"]["status"]),
        "r_delta": round(sum((c["now"]["r_multiple"] or 0) - (c["was"]["r_multiple"] or 0)
                             for c in changed), 3),
        "changes": changed,
        "dry_run": dry_run,
        "learning_untouched": True,
    }
    if dry_run or not changed:
        report["balance_before"] = PaperAccount(store, start_balance, risk_pct).balance
        report["balance_after"] = None
        return report

    before = PaperAccount(store, start_balance, risk_pct).balance
    store.write(OUTCOMES_KEY, updated)
    report["balance_after"] = replay_balance(store, start_balance, risk_pct)
    report["balance_before"] = before
    log.info(
        "Rebank: %d of %d outcomes re-booked (%d changed status), balance %.2f -> %.2f",
        len(changed), len(rows), report["status_changed"], before, report["balance_after"],
    )
    return report


def replay_balance(store: StateStore, start_balance: float = 1000.0,
                   risk_pct: float = 1.0) -> float:
    """Rebuild the paper balance from the outcome log, in resolution order.

    The balance compounds — each trade risks a share of the balance *at that
    time* — so it cannot be corrected in place by adding a delta. It has to be
    replayed, and in the order the trades actually resolved.
    """
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
