"""Signal outcome tracker — the core of the bot.

Every signal the bot emits is recorded as PENDING. On each tracking cycle the
tracker replays the 15m candles since the signal was created and advances its
lifecycle:

    PENDING --(price touches entry)--> ACTIVE --(TP rungs)--> TP_HIT
                                             \\--(stop)------> SL_HIT
    PENDING --(entry never touched, timeout)-> INVALIDATED
    ACTIVE  --(timeout, in profit/loss)------> EXPIRED_WIN / EXPIRED_LOSS

Design notes (improvements over the previous bot):
* No module-level mutable state. The :class:`Tracker` owns its dependencies
  (state store, exchange client, settings) which are injected explicitly, so it
  can be constructed in a test with fakes.
* All persistence goes through :class:`~wolf.state.StateStore` (atomic + locked).
* Per-signal evaluation is wrapped so a single bad symbol cannot wedge the whole
  batch — every other signal still resolves and state is still saved.
* Exceptions are caught narrowly and logged, never silently swallowed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from wolf.config import TrackerSettings
from wolf.exchange import BinanceClient
from wolf.models import Direction, EntryMode, Signal, Status, TpRung
from wolf.state import StateStore

log = logging.getLogger("wolf.tracker")

PENDING_KEY = "pending_signals"
OUTCOMES_KEY = "signal_outcomes"

# Notify callback: (signal, event, info) -> None. ``event`` is one of
# ACTIVATED | TP_HIT | RESOLVED.
NotifyFn = Callable[[Signal, str, dict], None]


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_ladder(
    tps: Optional[list[dict]],
    tp: float,
    sl: float,
    entry: float,
    is_long: bool,
) -> list[TpRung]:
    """Build a clean TP ladder ordered nearest-to-entry first.

    Rungs on the wrong side of entry (i.e. not in profit territory) are dropped.
    If no valid ladder is supplied, falls back to a single rung at ``tp``.
    """
    rungs: list[TpRung] = []
    for raw in tps or []:
        try:
            price = float(raw["price"])
            level = int(raw.get("level", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if not price or not level:
            continue
        if (is_long and price > entry) or (not is_long and price < entry):
            rungs.append(TpRung(level=level, price=price))
    if not rungs and tp:
        if (is_long and tp > entry) or (not is_long and tp < entry):
            rungs.append(TpRung(level=1, price=float(tp)))
    rungs.sort(key=lambda r: r.price, reverse=not is_long)
    for i, rung in enumerate(rungs, start=1):
        rung.level = i
    return rungs


class EvalResult:
    """Outcome of replaying candles for one signal."""

    __slots__ = (
        "activated",
        "activated_time",
        "tps_hit",
        "tps_meta",
        "terminal",
        "exit_price",
        "exit_time",
    )

    def __init__(self) -> None:
        self.activated: bool = False
        self.activated_time: Optional[datetime] = None
        self.tps_hit: list[int] = []
        self.tps_meta: dict[int, tuple[float, datetime]] = {}
        self.terminal: Optional[Status] = None
        self.exit_price: Optional[float] = None
        self.exit_time: Optional[datetime] = None


class Tracker:
    def __init__(
        self,
        store: StateStore,
        client: BinanceClient,
        settings: TrackerSettings,
        notify: Optional[NotifyFn] = None,
        account=None,
    ) -> None:
        self._store = store
        self._client = client
        self._settings = settings
        self._notify = notify or (lambda *_: None)
        self._account = account  # optional PaperAccount for the Trade Report
        # Guards the compound read-modify-write of pending_signals. StateStore
        # makes each read/write atomic, but record_signal and check_pending each
        # do load -> mutate -> save, which would otherwise interleave when the
        # scheduler runs the `scan` (record) and `track` (check) jobs on separate
        # threads — a lost-update race. This lock serialises those critical
        # sections; reads (active_signals/outcomes/stats) stay lock-free.
        self._lock = threading.RLock()

    # ── persistence helpers ────────────────────────────────────────────
    def _load_pending(self) -> list[Signal]:
        raw = self._store.read(PENDING_KEY, default=[])
        return [Signal.from_dict(d) for d in raw if isinstance(d, dict)]

    def _save_pending(self, signals: list[Signal]) -> None:
        self._store.write(PENDING_KEY, [s.to_dict() for s in signals])

    def _append_outcome(self, signal: Signal) -> None:
        cap = self._settings.max_outcomes
        self._store.update(
            OUTCOMES_KEY,
            lambda cur: ((cur or []) + [signal.to_dict()])[-cap:],
            default=[],
        )

    # ── recording ──────────────────────────────────────────────────────
    def record_signal(
        self,
        symbol: str,
        signal_type: str,
        direction: str,
        entry_price: float,
        tp: float,
        sl: float,
        score: int = 0,
        confluence_level: str = "",
        reasons: Optional[list[str]] = None,
        strategy: str = "CONFIRMED",
        entry_mode: str = EntryMode.RETEST_WAIT.value,
        tps: Optional[list[dict]] = None,
    ) -> Optional[Signal]:
        """Record a freshly-emitted signal as PENDING.

        Returns the stored :class:`Signal`, or ``None`` if the signal was
        rejected (bad prices) or deduplicated.
        """
        try:
            entry = float(entry_price)
            tp_f = float(tp)
            sl_f = float(sl)
        except (TypeError, ValueError):
            log.warning("Reject %s %s: non-numeric prices", symbol, direction)
            return None

        if entry <= 0 or tp_f <= 0 or sl_f <= 0:
            log.warning("Reject %s %s: non-positive prices", symbol, direction)
            return None

        is_long = direction.upper() == Direction.LONG.value
        # Sanity: TP/SL must be on the correct side of entry, otherwise an
        # outcome could be mis-marked as a win when it's actually a loss.
        if is_long and not (tp_f > entry > sl_f):
            log.warning("Reject %s LONG: need tp>entry>sl (%.6g/%.6g/%.6g)", symbol, tp_f, entry, sl_f)
            return None
        if not is_long and not (tp_f < entry < sl_f):
            log.warning("Reject %s SHORT: need tp<entry<sl (%.6g/%.6g/%.6g)", symbol, tp_f, entry, sl_f)
            return None

        ladder = normalize_ladder(tps, tp_f, sl_f, entry, is_long)
        signal = Signal(
            symbol=symbol,
            signal_type=signal_type,
            direction=direction.upper(),
            entry_price=entry,
            tp=tp_f,
            sl=sl_f,
            score=int(score),
            confluence_level=confluence_level,
            reasons=reasons or [],
            strategy=strategy,
            entry_mode=(entry_mode or EntryMode.RETEST_WAIT.value).upper(),
            tp_ladder=[r.to_dict() for r in ladder],
            timeout_hours=self._settings.timeout_for(signal_type),
        )

        with self._lock:
            pending = self._load_pending()
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=self._settings.dedup_minutes)
            for existing in pending:
                if (
                    existing.symbol == symbol
                    and existing.direction == signal.direction
                    and existing.status in (Status.PENDING.value, Status.ACTIVE.value)
                ):
                    try:
                        if _parse_iso(existing.created_at) > cutoff:
                            log.debug("Dedup %s %s within %dm", symbol, direction, self._settings.dedup_minutes)
                            return None
                    except ValueError:
                        continue

            pending.append(signal)
            self._save_pending(pending)
        log.info(
            "Tracked %s %s @ %.6g | TP %.6g SL %.6g",
            symbol, signal.direction, entry, tp_f, sl_f,
        )
        return signal

    # ── evaluation ──────────────────────────────────────────────────────
    def _evaluate(self, sig: Signal, future: list, created_at: datetime, now: datetime) -> EvalResult:
        res = EvalResult()
        is_long = sig.is_long
        entry = sig.entry_price
        ladder = normalize_ladder(sig.tp_ladder, sig.tp, sig.sl, entry, is_long)
        momentum = sig.entry_mode.upper() == EntryMode.MOMENTUM_NOW.value

        res.activated = bool(sig.activated) or momentum
        res.activated_time = created_at if res.activated else None
        eff_sl = sig.sl
        first_lvl = ladder[0].level if ladder else None

        for c in future:
            c_time = datetime.fromtimestamp(c.time / 1000, tz=timezone.utc)

            if not res.activated:
                touched = (is_long and c.low <= entry) or (not is_long and c.high >= entry)
                if touched:
                    res.activated = True
                    res.activated_time = c_time
                else:
                    continue

            # Stop-loss is checked first (conservative).
            sl_hit = (is_long and c.low <= eff_sl) or (not is_long and c.high >= eff_sl)
            if sl_hit:
                res.terminal = Status.SL_HIT
                res.exit_price = eff_sl
                res.exit_time = c_time
                break

            for rung in ladder:
                if rung.level in res.tps_hit:
                    continue
                hit = (is_long and c.high >= rung.price) or (not is_long and c.low <= rung.price)
                if hit:
                    res.tps_hit.append(rung.level)
                    res.tps_meta[rung.level] = (rung.price, c_time)
                    if rung.level == first_lvl:
                        eff_sl = entry  # move stop to breakeven after TP1

            if ladder and len(res.tps_hit) >= len(ladder):
                res.terminal = Status.TP_HIT
                res.exit_price = ladder[-1].price
                res.exit_time = c_time
                break

        if res.terminal is None:
            age_hours = (now - created_at).total_seconds() / 3600
            if age_hours >= sig.timeout_hours:
                if not res.activated:
                    res.terminal = Status.INVALIDATED
                    res.exit_price = entry
                    res.exit_time = now
                else:
                    curr = self._client.get_price(sig.symbol)
                    if curr:
                        pnl = (curr - entry) if is_long else (entry - curr)
                        res.terminal = Status.EXPIRED_WIN if pnl > 0 else Status.EXPIRED_LOSS
                        res.exit_price = curr
                    else:
                        res.terminal = Status.EXPIRED
                        res.exit_price = entry
                    res.exit_time = now
        return res

    def check_pending(self) -> list[Signal]:
        """Advance every pending/active signal; return the resolved ones.

        The load -> evaluate -> save of the pending list runs under the tracker
        lock so a concurrent ``record_signal`` (from the scan job or the API)
        cannot have its append clobbered by this method's save.
        """
        now = datetime.now(timezone.utc)
        still_pending: list[Signal] = []
        resolved: list[Signal] = []
        pending_notifications: list[tuple[Signal, str, dict]] = []

        with self._lock:
            pending = self._load_pending()
            for sig in pending:
                if sig.status not in (Status.PENDING.value, Status.ACTIVE.value):
                    continue
                try:
                    created_at = _parse_iso(sig.created_at)
                except (ValueError, TypeError) as exc:
                    log.warning("Bad created_at for %s: %s — keeping pending", sig.symbol, exc)
                    still_pending.append(sig)
                    continue

                age_hours = (now - created_at).total_seconds() / 3600
                try:
                    candles = self._client.get_klines(
                        sig.symbol, interval="15m", limit=int(max(age_hours + 1, 4) * 4) + 10
                    )
                    created_ts = int(created_at.timestamp() * 1000)
                    future = [c for c in candles if c.time > created_ts]
                    res = self._evaluate(sig, future, created_at, now)
                except (KeyError, ValueError, TypeError) as exc:
                    log.warning("Eval failed for %s: %s — keeping pending", sig.symbol, exc)
                    still_pending.append(sig)
                    continue

                prev_activated = bool(sig.activated)
                prev_tps = set(sig.tps_hit)

                if res.activated and not prev_activated:
                    sig.activated = True
                    sig.activated_at = (res.activated_time or now).isoformat()
                    sig.status = Status.ACTIVE.value
                    pending_notifications.append((sig, "ACTIVATED", {"price": sig.entry_price}))

                ladder_n = len(sig.tp_ladder or [])
                for lvl in res.tps_hit:
                    if lvl in prev_tps:
                        continue
                    if res.terminal == Status.TP_HIT and lvl == ladder_n:
                        continue  # final rung is reported by the resolution notif
                    price_t = res.tps_meta.get(lvl, (None, None))[0]
                    pending_notifications.append((sig, "TP_HIT", {"level": lvl, "price": price_t}))
                sig.tps_hit = res.tps_hit

                if res.terminal is None:
                    sig.status = Status.ACTIVE.value if res.activated else Status.PENDING.value
                    still_pending.append(sig)
                    continue

                self._resolve(sig, res, created_at, now)
                resolved.append(sig)

            self._save_pending(still_pending)
            for sig in resolved:
                self._append_outcome(sig)

        # Notifications are I/O and don't touch state — fire them outside the lock.
        for sig, event, info in pending_notifications:
            self._safe_notify(sig, event, info)
        for sig in resolved:
            self._safe_notify(sig, "RESOLVED", self._resolution_info(sig))
        return resolved

    def _resolution_info(self, sig: Signal) -> dict:
        """Trade-Report payload: paper-account move + a learned-edge note."""
        info: dict = {"lesson": self._lesson(sig)}
        if self._account is not None:
            try:
                snapshot = self._account.apply(sig)
            except Exception:  # the account must never break tracking/notify
                log.exception("Paper account update failed for %s", sig.symbol)
                snapshot = None
            if snapshot:
                info.update(snapshot)
        return info

    def _lesson(self, sig: Signal) -> str:
        """One-line takeaway from the cumulative record for this strategy."""
        bucket = self.stats().get("by_strategy", {}).get(sig.strategy)
        if not bucket or not bucket.get("total"):
            return f"{sig.strategy}: first graded trade — building a baseline."
        wr = bucket["win_rate"]
        n = bucket["total"]
        avg = bucket["avg_pnl"]
        if wr >= 55 and avg > 0:
            verdict = "edge holding — keep taking these"
        elif wr >= 45:
            verdict = "roughly coin-flip — needs tighter filters"
        else:
            verdict = "underperforming — tighten or pause this setup"
        return f"{sig.strategy}: {wr:.0f}% win over {n} ({avg:+.2f}% avg) — {verdict}."

    def _resolve(self, sig: Signal, res: EvalResult, created_at: datetime, now: datetime) -> None:
        exit_price = res.exit_price
        exit_time = res.exit_time or now
        hold_hours = (exit_time - created_at).total_seconds() / 3600
        if exit_price and sig.entry_price > 0 and res.terminal != Status.INVALIDATED:
            pnl = (
                (exit_price - sig.entry_price) / sig.entry_price * 100
                if sig.is_long
                else (sig.entry_price - exit_price) / sig.entry_price * 100
            )
        else:
            pnl = 0.0
        sig.status = res.terminal.value
        sig.exit_price = exit_price
        sig.exit_time = exit_time.isoformat()
        sig.pnl_pct = round(pnl, 3)
        sig.hold_hours = round(hold_hours, 2)
        sig.tps_hit = res.tps_hit
        sig.resolved_at = now.isoformat()
        log.info("Resolved %s %s -> %s | PnL %+.2f%%", sig.symbol, sig.direction, sig.status, pnl)

    def _safe_notify(self, sig: Signal, event: str, info: dict) -> None:
        try:
            self._notify(sig, event, info)
        except Exception:  # notification must never break tracking
            log.exception("Notification callback failed for %s/%s", sig.symbol, event)

    # ── queries ─────────────────────────────────────────────────────────
    def active_signals(self) -> list[Signal]:
        return [
            s for s in self._load_pending()
            if s.status in (Status.PENDING.value, Status.ACTIVE.value)
        ]

    def outcomes(self) -> list[Signal]:
        raw = self._store.read(OUTCOMES_KEY, default=[])
        return [Signal.from_dict(d) for d in raw if isinstance(d, dict)]

    def stats(self) -> dict:
        """Aggregate win-rate / PnL stats over resolved outcomes."""
        outcomes = self.outcomes()
        graded = [o for o in outcomes if Status(o.status).is_win or Status(o.status).is_loss]
        wins = [o for o in graded if Status(o.status).is_win]
        total = len(graded)
        win_rate = (len(wins) / total * 100) if total else 0.0
        pnls = [o.pnl_pct for o in graded if o.pnl_pct is not None]
        avg_pnl = (sum(pnls) / len(pnls)) if pnls else 0.0

        by_strategy: dict[str, dict] = {}
        for o in graded:
            bucket = by_strategy.setdefault(o.strategy, {"wins": 0, "total": 0, "pnl": 0.0})
            bucket["total"] += 1
            bucket["pnl"] += o.pnl_pct or 0.0
            if Status(o.status).is_win:
                bucket["wins"] += 1
        for bucket in by_strategy.values():
            bucket["win_rate"] = round(bucket["wins"] / bucket["total"] * 100, 1) if bucket["total"] else 0.0
            bucket["avg_pnl"] = round(bucket["pnl"] / bucket["total"], 3) if bucket["total"] else 0.0

        return {
            "total_resolved": len(outcomes),
            "total_graded": total,
            "wins": len(wins),
            "losses": total - len(wins),
            "win_rate": round(win_rate, 1),
            "avg_pnl_pct": round(avg_pnl, 3),
            "active": len(self.active_signals()),
            "by_strategy": by_strategy,
        }
