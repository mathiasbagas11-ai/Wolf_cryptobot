"""Diagnostics — the derived statistics behind a performance verdict.

The Telegram card answers "how did it go". This module answers "does that mean
anything", which is a different question and the one that kept being re-derived
by hand from a lossy summary.

Four things the aggregate card cannot say, and this can:

* **How noisy the average is.** An expectancy without its standard error is not
  evidence. Reporting a mean of +0.085R over 226 trades sounds like an edge; the
  same number with se 0.094 is a coin flip.
* **What "no edge" would have looked like.** A win rate is meaningless against
  50%: a ladder with a 1.5R stop and a 3R target pays out ~25% of the time under
  a driftless random walk. The baseline is derived per strategy from the
  geometry those trades actually carried.
* **What the trade cost.** Targets are ATR multiples, so 1R is often well under
  1% and round-trip fees can exceed the gross edge outright. Expectancy is
  reported net.
* **How many independent observations there really are.** Positions held
  simultaneously in a correlated market are not independent samples, so the
  nominal count overstates the evidence.

Verdicts are deliberately hard to earn: below ``MIN_CONCLUSIVE_TRADES`` graded
trades, or |t| under ``CONCLUSIVE_T``, the answer is INCONCLUSIVE regardless of
how the sample looks. With samples this size that is usually the correct answer,
and a diagnostic that cannot say "I don't know" is a machine for manufacturing
confidence.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from wolf.config import LadderSettings, state_is_persistent, volume_mount
from wolf.models import Signal, Status
from wolf.tracker import Tracker, _parse_iso, _risk_pct, r_multiple_of

# A verdict needs both a real sample and a real signal-to-noise ratio.
MIN_CONCLUSIVE_TRADES = 100
CONCLUSIVE_T = 2.0
# Below this fraction of the whole sample's spread, a bucket's own standard
# deviation is treated as collapsed rather than small — see _summarise.
DEGENERATE_SD_FRACTION = 0.25

# One-sided 95% bound, matching the auto-pause gate.
CI_Z = 1.96

# Verdicts
INCONCLUSIVE = "INCONCLUSIVE"
EDGE_POSITIVE = "EDGE_POSITIVE"
EDGE_NEGATIVE = "EDGE_NEGATIVE"
NET_NEGATIVE_AFTER_COST = "NET_NEGATIVE_AFTER_COST"


def _safe_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _parse_iso(value)
    except (ValueError, TypeError):
        return None


def _no_edge_win_rate(sig: Signal, tp1_banks_win: bool) -> Optional[float]:
    """Win rate this trade's own geometry would produce with no edge at all.

    Under a driftless walk the chance of touching barrier A before barrier B is
    B/(A+B). With the stop trailing to entry after the first rung:

        P(TP1 first)      = d_sl / (d_sl + d_tp1)
        P(final | TP1)    = d_tp1 / d_tp_last

    When TP1 banks a partial win, reaching TP1 is already a win; otherwise only
    the full ladder counts. Returns ``None`` when the geometry is unusable.
    """
    entry, sl = sig.entry_price, sig.sl
    if entry <= 0 or sl <= 0:
        return None
    rungs = sorted(
        (float(r["price"]) for r in (sig.tp_ladder or []) if r.get("price")),
        reverse=not sig.is_long,
    )
    if not rungs:
        return None
    d_sl = abs(entry - sl)
    d_tp1 = abs(rungs[0] - entry)
    d_last = abs(rungs[-1] - entry)
    if d_sl <= 0 or d_tp1 <= 0 or d_last <= 0:
        return None

    p_tp1 = d_sl / (d_sl + d_tp1)
    if tp1_banks_win:
        return p_tp1 * 100
    return p_tp1 * (d_tp1 / d_last) * 100


def _concurrency(outcomes: Iterable[Signal]) -> dict:
    """How many positions were open at once, as a check on independence.

    Trades running side by side in a correlated market share most of their move,
    so the nominal count overstates how much was actually observed. Sweeping the
    open/close events gives the average overlap; dividing by it yields the
    worst case (perfectly correlated) — a floor on the effective sample, not an
    estimate of it.
    """
    events: list[tuple[datetime, int]] = []
    for o in outcomes:
        start = _safe_dt(o.activated_at) or _safe_dt(o.created_at)
        end = _safe_dt(o.resolved_at) or _safe_dt(o.exit_time)
        if start and end and end >= start:
            events.append((start, 1))
            events.append((end, -1))
    if not events:
        return {"mean_open": 0.0, "max_open": 0, "eff_n_floor": 0}

    events.sort(key=lambda e: e[0])
    open_now = 0
    peak = 0
    weighted = 0.0
    span = 0.0
    prev = events[0][0]
    for ts, delta in events:
        gap = (ts - prev).total_seconds()
        if gap > 0 and open_now > 0:
            weighted += open_now * gap
            span += gap
        open_now += delta
        peak = max(peak, open_now)
        prev = ts
    mean_open = (weighted / span) if span else 0.0
    n = len(events) // 2
    return {
        "mean_open": round(mean_open, 2),
        "max_open": peak,
        "eff_n_floor": int(n / mean_open) if mean_open >= 1 else n,
    }


def _ladder_economics(traded: list) -> dict:
    """How far the ladder actually runs, and what that demands of the win rate.

    A reward:risk ratio describes the *last* rung. What decides whether the
    system makes money is the average R of a winner, and with a front-loaded
    scale-out plus a breakeven stop those are very different numbers: banking
    50% at 1R and scratching the rest returns +0.5R, not the ladder's 1.7R
    ceiling. Quoting the ceiling understates the required win rate by half.

    So the breakeven win rate here is derived from realised wins and losses
    rather than from the configured geometry. It answers "what would this have
    needed", which is the only version that can be checked against the win rate
    actually achieved.
    """
    wins = [r_multiple_of(o) for o in traded if r_multiple_of(o) > 0]
    losses = [-r_multiple_of(o) for o in traded if r_multiple_of(o) < 0]
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0

    # How deep into the ladder the winners got. A runner that never pays means
    # the far rungs are decoration and the geometry should be re-cut.
    filled: dict[int, int] = {}
    for o in traded:
        for level in (o.tps_hit or []):
            filled[level] = filled.get(level, 0) + 1
    n = len(traded)

    breakeven_wr = (avg_loss / (avg_win + avg_loss) * 100) if (avg_win + avg_loss) else 0.0
    return {
        "avg_win_r": round(avg_win, 3),
        "avg_loss_r": round(avg_loss, 3),
        "breakeven_win_rate": round(breakeven_wr, 1),
        "rung_fill_rate": {
            f"tp{lvl}": round(filled.get(lvl, 0) / n * 100, 1) for lvl in (1, 2, 3)
        } if n else {},
    }


def _summarise(rs: list[float], cost_r: float, sd_floor: float = 0.0) -> dict:
    """Mean, spread and verdict for one population of R-multiples.

    ``sd_floor`` guards against a spread that collapsed rather than one that is
    genuinely small. This ladder quantises its outcomes — a stop pays -1.0R, a
    banked TP1 pays 0.5R, a full run 1.7R — so a handful of trades landing on
    the same rung is ordinary, and it breaks the t-statistic in both
    directions at once. Three SWING trades all at exactly -1.0R gave sd=0, se=0
    and t=0, reading as "no evidence" for a sample that lost unanimously; two
    PREDUMP trades at 0.499 and 0.500 gave sd=0.001 and t=999, reading as
    overwhelming proof of an edge.

    Neither sample knows anything about variance. Substituting the spread of
    the whole traded sample says so honestly: these outcomes vary about as much
    as every other bucket's, and this one is simply too small to have shown it.
    """
    n = len(rs)
    if n == 0:
        return {
            "n": 0, "mean_r": 0.0, "sd_r": 0.0, "se_r": 0.0, "t": 0.0,
            "ci95": [0.0, 0.0], "net_r": 0.0, "verdict": INCONCLUSIVE,
        }
    mean = statistics.fmean(rs)
    sd = statistics.stdev(rs) if n > 1 else 0.0
    # Only a collapsed spread is replaced; a merely narrow one is left alone.
    eff_sd = max(sd, sd_floor) if sd < sd_floor * DEGENERATE_SD_FRACTION else sd
    se = eff_sd / math.sqrt(n) if eff_sd else 0.0
    t = mean / se if se else 0.0
    half = CI_Z * se
    net = mean - cost_r

    if n < MIN_CONCLUSIVE_TRADES or abs(t) < CONCLUSIVE_T:
        verdict = INCONCLUSIVE
    elif mean < 0:
        verdict = EDGE_NEGATIVE
    elif net < 0:
        # A gross edge that does not survive its own trading costs.
        verdict = NET_NEGATIVE_AFTER_COST
    else:
        verdict = EDGE_POSITIVE

    return {
        "n": n,
        "mean_r": round(mean, 3),
        "sd_r": round(sd, 3),
        "se_r": round(se, 3),
        "t": round(t, 2),
        "ci95": [round(mean - half, 3), round(mean + half, 3)],
        "net_r": round(net, 3),
        "verdict": verdict,
    }


def diagnose(
    tracker: Tracker,
    *,
    window_hours: Optional[float] = None,
    round_trip_bps: float = 20.0,
    tp1_banks_win: bool = False,
    state_dir: str = "",
    ai_available: Optional[bool] = None,
) -> dict:
    """Compute the full diagnostic over resolved outcomes.

    ``round_trip_bps`` is the assumed all-in cost of a trade (taker fees both
    sides plus slippage). It is converted into R using the median risk distance,
    because that is the unit the strategies actually target.
    """
    outcomes = tracker.outcomes()
    if window_hours and window_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        outcomes = [
            o for o in outcomes
            if (ts := (_safe_dt(o.resolved_at) or _safe_dt(o.exit_time))) is not None
            and ts >= cutoff
        ]

    traded = [o for o in outcomes if o.status != Status.INVALIDATED.value]
    graded = [o for o in traded if Status(o.status).is_graded]

    def _cost_r(rows: list) -> tuple[float, float]:
        """Return ``(median 1R %, cost in R)`` for one population.

        Cost has to be computed from *that* population's own risk unit. A
        strategy stopping 0.12% from entry pays 1.67R in fees on a 20bps round
        trip, while one stopping 0.80% away pays 0.25R — charging both the
        portfolio median inverts their ranking outright, which is exactly what
        it did: the strategy reported as the only net-positive one was in fact
        paying fourteen times its own risk unit more than the report showed.
        """
        risks = [r for r in (_risk_pct(o) for o in rows) if r > 0]
        median = statistics.median(risks) if risks else 0.0
        return median, ((round_trip_bps / 100.0) / median if median else 0.0)

    median_1r, cost_r = _cost_r(traded)

    overall = _summarise([r_multiple_of(o) for o in traded], cost_r)
    # The whole sample's spread is the reference every bucket is measured
    # against, so a bucket whose own spread collapsed borrows this one.
    sample_sd = overall["sd_r"]

    by_strategy: dict[str, dict] = {}
    for name in sorted({o.strategy for o in traded}):
        rows = [o for o in traded if o.strategy == name]
        graded_rows = [o for o in rows if Status(o.status).is_graded]
        strat_1r, strat_cost_r = _cost_r(rows)
        summary = _summarise([r_multiple_of(o) for o in rows], strat_cost_r, sample_sd)
        summary["cost_r"] = round(strat_cost_r, 3)

        wins = sum(1 for o in graded_rows if Status(o.status).is_win)
        wr = (wins / len(graded_rows) * 100) if graded_rows else 0.0
        baselines = [
            b for b in (_no_edge_win_rate(o, tp1_banks_win) for o in rows) if b is not None
        ]
        no_edge = statistics.fmean(baselines) if baselines else None
        # How far the observed win rate sits from its own no-edge baseline, in
        # binomial standard deviations. Answers "is 33% good?" — which depends
        # entirely on the ladder, not on 50%.
        wr_z = None
        if no_edge is not None and graded_rows:
            p = no_edge / 100
            sd = math.sqrt(len(graded_rows) * p * (1 - p))
            if sd > 0:
                wr_z = round((wins - len(graded_rows) * p) / sd, 2)

        summary.update({
            "graded": len(graded_rows),
            "win_rate": round(wr, 1),
            "no_edge_win_rate": round(no_edge, 1) if no_edge is not None else None,
            "win_rate_z": wr_z,
            "median_1r_pct": round(strat_1r, 3),
        })
        by_strategy[name] = summary

    # On-chain dimensions: is any of the collected data actually predictive?
    #
    # Same statistics as by_strategy, because the question is the same one — does
    # this population's mean R differ from zero by enough to act on. Today only
    # the whale gate acts on any of it; these buckets are what should decide
    # whether the others earn a gate too, or whether the whale threshold is set
    # anywhere near right.
    #
    # NO_DATA stays its own bucket: a collector that was off is not a finding,
    # and folding it into NEUTRAL would dilute whatever real effect exists.
    def _onchain_buckets(key: str) -> dict[str, dict]:
        buckets: dict[str, dict] = {}
        for label in sorted({(getattr(o, key, "") or "NO_DATA") for o in traded}):
            rows = [o for o in traded if (getattr(o, key, "") or "NO_DATA") == label]
            graded_rows = [o for o in rows if Status(o.status).is_graded]
            _, bucket_cost_r = _cost_r(rows)
            summary = _summarise([r_multiple_of(o) for o in rows], bucket_cost_r, sample_sd)
            wins = sum(1 for o in graded_rows if Status(o.status).is_win)
            summary.update({
                "graded": len(graded_rows),
                "win_rate": round(wins / len(graded_rows) * 100, 1) if graded_rows else 0.0,
            })
            buckets[label] = summary
        return buckets

    by_whale_stance = _onchain_buckets("whale_stance")
    by_onchain_bias = _onchain_buckets("onchain_bias")

    # Whale stance crossed with strategy, because the one-dimensional split
    # cannot tell a real effect from a bookkeeping artefact. If the strategies
    # that lose are also the ones whose signals whales happen to agree with,
    # "trading with whales loses" is just those strategies losing, re-labelled.
    # Only the cross-tab separates the two, and the answer decides whether the
    # whale veto is filtering the right side.
    def _whale_by_strategy() -> dict[str, dict]:
        out: dict[str, dict] = {}
        for strategy in sorted({o.strategy or "?" for o in traded}):
            rows = [o for o in traded if (o.strategy or "?") == strategy]
            cells: dict[str, dict] = {}
            for stance in sorted({(o.whale_stance or "NO_DATA") for o in rows}):
                cell = [o for o in rows if (o.whale_stance or "NO_DATA") == stance]
                graded_cell = [o for o in cell if Status(o.status).is_graded]
                wins = sum(1 for o in graded_cell if Status(o.status).is_win)
                cells[stance] = {
                    "n": len(cell),
                    "graded": len(graded_cell),
                    "win_rate": round(wins / len(graded_cell) * 100, 1) if graded_cell else 0.0,
                    "mean_r": round(
                        statistics.fmean([r_multiple_of(o) for o in cell]), 3
                    ) if cell else 0.0,
                }
            out[strategy] = cells
        return out

    whale_by_strategy = _whale_by_strategy()

    status_counts: dict[str, int] = {}
    for o in outcomes:
        status_counts[o.status] = status_counts.get(o.status, 0) + 1

    ai_verdicts: dict[str, int] = {}
    for o in traded:
        key = o.ai_verdict or "NO_AI"
        ai_verdicts[key] = ai_verdicts.get(key, 0) + 1
    abstain_rate = (
        (ai_verdicts.get("ABSTAIN", 0) + ai_verdicts.get("NO_AI", 0)) / len(traded)
        if traded else 0.0
    )

    ladder = _ladder_economics(traded)

    flags = []
    # Same durability test the startup card uses. The old check here only asked
    # whether the path was absolute, which passes /app/state_data — absolute and
    # still inside the container, so it is wiped exactly like a relative path.
    if state_dir and not state_is_persistent(state_dir):
        flags.append("STATE_NOT_PERSISTED")
    if traded and abstain_rate >= 0.99:
        # Name the fault, not just the symptom. Every one of these degrades to
        # ABSTAIN precisely so it cannot break screening, which also means it
        # cannot be seen: a rejected key, a spent balance and a model that
        # cannot return JSON all read as an AI with no opinion.
        reasons: dict[str, int] = {}
        for o in traded:
            head, _, rest = (o.ai_rationale or "").partition(":")
            if head.strip().startswith("ABSTAIN/"):
                # Keep the provider's own sentence, not just the class of fault.
                # "NO_JSON" says the arbiter did not return a verdict; only the
                # message says whether that was an expired key, a spent balance
                # or a model answering in prose — three different remedies, and
                # two of them are not code changes at all.
                key = head.strip().split("/", 1)[1]
                if rest.strip():
                    key = f"{key}: {rest.strip()}"
                reasons[key] = reasons.get(key, 0) + 1
        detail = " | ".join(
            f"{k} x{v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        )
        flags.append(f"AI_NEVER_DECIDES({detail})" if detail else "AI_NEVER_DECIDES")
    if ai_available is False:
        flags.append("AI_CLIENT_UNAVAILABLE")
    unpriced = status_counts.get(Status.EXPIRED.value, 0)
    if unpriced:
        flags.append(f"UNPRICED_EXITS={unpriced}")
    if not tp1_banks_win:
        flags.append("TP1_BANKS_WIN_OFF")
    # Scaling out caps a perfect run: most of the size is sold before the last
    # rung, so no single trade can pay what the final rung is worth. An average
    # winner above that ceiling is arithmetically impossible, which means the
    # grading is wrong — not that the strategy is exceptional. Say so loudly,
    # because every number downstream inherits the error and they all look good.
    ceiling = LadderSettings().full_run_r
    if ladder["avg_win_r"] > ceiling:
        flags.append(f"AVG_WIN_ABOVE_LADDER_CEILING={ladder['avg_win_r']:.2f}R>{ceiling:.2f}R")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_hours": window_hours,
        "sample": {
            "traded": len(traded),
            "graded": len(graded),
            "flat": status_counts.get(Status.EXPIRED_FLAT.value, 0),
            "invalidated": status_counts.get(Status.INVALIDATED.value, 0),
            "unpriced": unpriced,
        },
        "cost": {
            "round_trip_bps": round_trip_bps,
            "median_1r_pct": round(median_1r, 3),
            "cost_r": round(cost_r, 3),
        },
        "overall": overall,
        "ladder": ladder,
        "by_strategy": by_strategy,
        "by_whale_stance": by_whale_stance,
        "whale_by_strategy": whale_by_strategy,
        "by_onchain_bias": by_onchain_bias,
        "status_counts": status_counts,
        "ai_verdicts": ai_verdicts,
        "concurrency": _concurrency(traded),
        "flags": flags,
        "state_dir": state_dir,
        "state_persistent": state_is_persistent(state_dir) if state_dir else None,
        "volume_mount": volume_mount(),
        "thresholds": {
            "min_conclusive_trades": MIN_CONCLUSIVE_TRADES,
            "conclusive_t": CONCLUSIVE_T,
        },
    }


def render_digest(diag: dict) -> str:
    """Render the diagnostic as a compact fixed-shape text block.

    Small enough to paste back into a conversation whole, and ordered so the
    verdict and the evidence behind it sit together — the numbers a reader would
    need to disagree with the verdict are on the same line as the verdict.
    """
    s, c, o = diag["sample"], diag["cost"], diag["overall"]
    win = diag.get("window_hours")
    lines = [
        f"WOLF-DIAG v1 | {diag['generated_at']} | window={f'{win:g}h' if win else 'all'}",
        f"sample   traded={s['traded']} graded={s['graded']} flat={s['flat']} "
        f"invalid={s['invalidated']} unpriced={s['unpriced']}",
        f"cost     {c['round_trip_bps']:g}bps / 1R={c['median_1r_pct']:.2f}% => {c['cost_r']:.3f}R",
        f"overall  meanR={o['mean_r']:+.3f} sdR={o['sd_r']:.2f} n={o['n']} se={o['se_r']:.3f} "
        f"t={o['t']:+.2f} ci95=[{o['ci95'][0]:+.3f},{o['ci95'][1]:+.3f}]",
        f"         netR={o['net_r']:+.3f}  => {o['verdict']}",
    ]
    for name, b in diag["by_strategy"].items():
        base = "n/a" if b["no_edge_win_rate"] is None else f"{b['no_edge_win_rate']:.1f}"
        z = "n/a" if b["win_rate_z"] is None else f"{b['win_rate_z']:+.2f}"
        lines.append(
            f"{name:<9} n={b['n']} graded={b['graded']} wr={b['win_rate']:.1f} "
            f"noedge_wr={base} wr_z={z}"
        )
        lines.append(
            f"{'':<9} meanR={b['mean_r']:+.3f} sd={b['sd_r']:.2f} t={b['t']:+.2f} "
            f"ci95=[{b['ci95'][0]:+.3f},{b['ci95'][1]:+.3f}] 1R={b['median_1r_pct']:.2f}% "
            f"cost={b.get('cost_r', 0):.2f}R netR={b['net_r']:+.3f} => {b['verdict']}"
        )
    # On-chain evidence. Printed only once a bucket exists, so a deployment with
    # the collectors off does not carry three lines of NO_DATA forever.
    for label, buckets in (("whale", diag.get("by_whale_stance") or {}),
                           ("onchain", diag.get("by_onchain_bias") or {})):
        real = {k: v for k, v in buckets.items() if k != "NO_DATA"}
        if not real:
            continue
        for name, b in buckets.items():
            lines.append(
                # Width fits the longest label the buckets can produce
                # ("onchain:SUPPORTS_SHORT"), so the columns stay aligned.
                f"{label + ':' + name:<22} n={b['n']} graded={b['graded']} "
                f"wr={b['win_rate']:.1f} meanR={b['mean_r']:+.3f} t={b['t']:+.2f} "
                f"=> {b['verdict']}"
            )

    # Cross-tab, printed only when a whale stance actually varies within a
    # strategy — that is the only case where it can separate a whale effect
    # from the strategy mix, and the only case worth the extra lines.
    cross = diag.get("whale_by_strategy") or {}
    informative = {
        name: cells for name, cells in cross.items()
        if len([k for k in cells if k != "NO_DATA"]) >= 2
    }
    for name, cells in informative.items():
        lines.append(
            f"wxs:{name:<18} " + "  ".join(
                f"{stance}(n={c['n']},wr={c['win_rate']:.0f},R={c['mean_r']:+.2f})"
                for stance, c in sorted(cells.items())
            )
        )

    if diag.get("state_dir"):
        mark = "ok" if diag.get("state_persistent") else "EPHEMERAL"
        mount = diag.get("volume_mount") or "none detected"
        lines.append(f"state    {diag['state_dir']} [{mark}] volume_mount={mount}")
    lad = diag.get("ladder") or {}
    if lad.get("avg_win_r") or lad.get("avg_loss_r"):
        fill = lad.get("rung_fill_rate") or {}
        lines.append(
            f"ladder   avgWin={lad['avg_win_r']:+.2f}R avgLoss=-{lad['avg_loss_r']:.2f}R "
            f"=> needs WR>{lad['breakeven_win_rate']:.1f}%  "
            + " ".join(f"{k}={v:.0f}%" for k, v in fill.items())
        )
    if diag["status_counts"]:
        lines.append(
            "status   " + " ".join(f"{k}={v}" for k, v in sorted(diag["status_counts"].items()))
        )
    conc = diag["concurrency"]
    lines.append(
        f"concur   mean_open={conc['mean_open']} max_open={conc['max_open']} "
        f"eff_n_floor={conc['eff_n_floor']}"
    )
    if diag["ai_verdicts"]:
        lines.append(
            "ai       " + " ".join(f"{k}={v}" for k, v in sorted(diag["ai_verdicts"].items()))
        )
    if diag["flags"]:
        lines.append("flags    " + " ".join(diag["flags"]))
    return "\n".join(lines)
