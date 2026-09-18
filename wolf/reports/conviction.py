"""AI conviction ranking → the High-Conviction topic.

Every other room answers "did something fire?". This one answers the question
that follows it: **of everything currently on the board, which one would you
actually take?** The bot can emit half a dozen live setups across a session and
they are not equal — a 4h SWING the debate confirmed at 80% and a 15m SCALP the
regime filter flagged both arrive as a green card in the New Signal room, and
nothing anywhere compares them.

The ranking is comparative on purpose. The debate layer judges each candidate in
isolation ("is this setup sound?"), which cannot say that setup A is the better
use of the same risk than setup B. So the whole live book goes into one prompt
and comes back ordered, with a thesis and the thing that would invalidate it per
pick.

Three rules this report is held to:

* **It never fetches.** Candidates come from the tracker's own pending book and
  the facts come off the recorded :class:`~wolf.models.Signal` — the same
  numbers that were true when the signal fired. A ranker that re-read the market
  would be answering a different question from the cards it is ranking, and
  would disagree with them in ways nobody could reconstruct.
* **It never invents a pick.** Every id the model returns is matched back to a
  real live signal; anything else is dropped. An LLM asked to rank five setups
  will occasionally return a sixth.
* **It says when the AI did not rank it.** With no usable client the picks are
  ordered by a documented heuristic instead, and the card says so in its header
  rather than passing a score sort off as a verdict. This codebase has been
  bitten by silent AI degradation more than once: an unavailable arbiter looks
  exactly like a healthy one that had no opinion.

The same set is not posted twice. A room that repeats yesterday's leaderboard
every hour is a room that stops being read, so the ordered pick ids are
remembered and an unchanged ranking is skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from wolf.market import age_minutes
from wolf.models import Signal, Status
from wolf.textfmt import DIVIDER, esc, fmt_price, now

log = logging.getLogger("wolf.reports")

#: Where the last posted ranking is remembered, so it is not re-sent unchanged.
STATE_KEY = "conviction_ranking"

#: Rank badges. Beyond the podium the plain number carries it.
_BADGES = ("🥇", "🥈", "🥉")

_RANKER_SYSTEM = (
    "You are the head trader of a crypto futures desk allocating ONE unit of "
    "risk. You are given every trade setup currently live on the desk. Rank "
    "them best-first by how well each would spend that risk — not by how "
    "exciting it is. Weigh evidence quality, reward-to-risk, timeframe "
    "coherence, whether the setup fights the broader market, and how much the "
    "recorded context supports the direction. Setups you would NOT take must be "
    "left out entirely rather than ranked last. For each pick give a conviction "
    "0-100, a one-sentence thesis for taking it, and the single thing that would "
    "invalidate it. Be selective: an honest short list beats a complete one."
)

_RANKING_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "conviction": {"type": "integer"},
                    "thesis": {"type": "string"},
                    "risk": {"type": "string"},
                },
                "required": ["id", "conviction", "thesis", "risk"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["picks"],
    "additionalProperties": False,
}


@dataclass
class RankedPick:
    """One entry of the leaderboard.

    ``conviction`` is the model's own 0-100 number and is meaningful only when
    ``source == "ai"``. The heuristic fallback leaves it at 0 and the card shows
    the signal's score instead, because presenting a sort key as a conviction is
    the exact misrepresentation this report is trying to avoid.
    """

    signal: Signal
    rank: int
    conviction: int = 0
    thesis: str = ""
    risk: str = ""
    source: str = "ai"          # "ai" | "heuristic"


def final_target(sig: Signal) -> float:
    """The furthest rung of the ladder, or ``tp`` when there is no ladder.

    Used for both the printed target and the R:R beside it, so the card cannot
    quote a ratio measured to one price and a target at another — which is
    exactly what happens when ``tp`` is read as "the target" on a laddered
    signal whose last rung sits somewhere else.
    """
    ladder = sig.tp_ladder or []
    if not ladder:
        return sig.tp
    try:
        return float(ladder[-1].get("price", sig.tp))
    except (TypeError, ValueError):
        return sig.tp


def rr_of(sig: Signal) -> float:
    """Reward:risk of the setup, measured to its furthest rung.

    Zero risk (a stop at the entry) can only come from corrupt state; it yields
    0.0 rather than dividing by nothing.
    """
    risk = abs(sig.entry_price - sig.sl)
    if not risk:
        return 0.0
    return abs(final_target(sig) - sig.entry_price) / risk


def heuristic_score(sig: Signal) -> float:
    """Fallback ordering when no model ranked the book.

    Deliberately plain arithmetic over what was already recorded: the detector's
    own score, adjusted by the signals this codebase has decided are worth
    flagging. It is a *sort key*, not a quality estimate — the card never quotes
    it as a conviction, and its only job is to keep the room useful on a day the
    AI provider is down.
    """
    value = float(sig.score)
    verdict = (sig.ai_verdict or "").upper()
    if verdict == "CONFIRM":
        value += 5.0 + sig.ai_confidence / 10.0
    elif verdict == "REJECT":
        value -= 15.0
    if sig.ai_vetoed:
        value -= 10.0
    if (sig.confluence_level or "").upper() == "HIGH":
        value += 5.0
    value += min(rr_of(sig), 4.0) * 3.0
    if sig.against_regime:
        value -= 10.0
    if sig.weak_strategy:
        value -= 10.0
    if sig.bounce_flagged:
        value -= 5.0
    stance = (sig.whale_stance or "").upper()
    if stance == "WITH":
        value += 5.0
    elif stance == "AGAINST":
        value -= 5.0
    return value


def _facts(sig: Signal, age_min: Optional[float]) -> str:
    """One setup, as the ranker sees it.

    Everything here was recorded at signal time. The risk flags and the on-chain
    stance are included precisely because they are the parts the individual card
    reports without weighing: a signal that fought the regime and one that ran
    with it both print as a signal.
    """
    lines = [
        f"id: {sig.id}",
        f"  symbol: {sig.symbol}  direction: {sig.direction}",
        f"  strategy: {sig.strategy} ({sig.signal_type})  timeframe: {sig.timeframe}",
        f"  entry: {sig.entry_price:.6g}  target: {final_target(sig):.6g}"
        f"  stop: {sig.sl:.6g}"
        f"  R:R {rr_of(sig):.2f}",
        f"  detector score: {sig.score}/100 ({sig.confluence_level or 'n/a'})",
        f"  status: {sig.status}"
        + (f"  age: {age_min:.0f}m" if age_min is not None else ""),
    ]
    if sig.ai_verdict and sig.ai_verdict != "ABSTAIN":
        rationale = f" — {sig.ai_rationale}" if sig.ai_rationale else ""
        lines.append(
            f"  debate verdict: {sig.ai_verdict} at {sig.ai_confidence}% confidence{rationale}"
        )
    flags = [
        label for label, on in (
            ("fights the market regime", sig.against_regime),
            ("emitted by an underperforming strategy", sig.weak_strategy),
            ("short into bounce/squeeze risk", sig.bounce_flagged),
        ) if on
    ]
    if flags:
        lines.append(f"  risk flags: {'; '.join(flags)}")
    if sig.whale_stance:
        lines.append(
            f"  tracked whales are {sig.whale_stance} this direction "
            f"({sig.whale_net_wallets} net wallets)"
        )
    if sig.onchain_bias:
        lines.append(f"  on-chain valuation bias: {sig.onchain_bias}")
    if sig.spread_bps is not None:
        lines.append(f"  top-of-book spread: {sig.spread_bps:.1f} bps")
    if sig.reasons:
        lines.append(f"  detector reasons: {'; '.join(sig.reasons)}")
    return "\n".join(lines)


class ConvictionRanker:
    """Ranks the live signal book and renders the High-Conviction card."""

    def __init__(
        self,
        tracker,
        store=None,
        llm=None,
        *,
        max_picks: int = 3,
        min_candidates: int = 2,
        min_conviction: int = 60,
        lookback_hours: float = 12.0,
        max_tokens: int = 1500,
        tz: str = "UTC",
    ) -> None:
        self._tracker = tracker
        self._store = store
        self._llm = llm
        self._max_picks = max_picks
        self._min_candidates = min_candidates
        self._min_conviction = min_conviction
        self._lookback_hours = lookback_hours
        self._max_tokens = max_tokens
        self._tz = tz

    @property
    def ai_available(self) -> bool:
        return self._llm is not None and bool(getattr(self._llm, "available", False))

    # ── candidates ────────────────────────────────────────────────────
    def candidates(self) -> list[Signal]:
        """Live setups worth comparing, freshest-looking first.

        Bounded by age because a ranking is an allocation decision and a setup
        from eleven hours ago is one whose entry the market has long since
        accepted or refused. Signals with an unreadable timestamp are kept: the
        tracker still considers them live, and dropping them here would quietly
        shrink the book for a reason that has nothing to do with the trade.
        """
        try:
            live = self._tracker.active_signals()
        except Exception:  # a state hiccup must not kill the report job
            log.warning("Could not read the live signal book for ranking", exc_info=True)
            return []
        cutoff = self._lookback_hours * 60.0
        fresh = []
        for sig in live:
            if sig.status not in (Status.PENDING.value, Status.ACTIVE.value):
                continue
            age = age_minutes(sig.created_at)
            if age is not None and age > cutoff:
                continue
            fresh.append(sig)
        return sorted(fresh, key=heuristic_score, reverse=True)

    # ── ranking ───────────────────────────────────────────────────────
    def rank(self, candidates: Sequence[Signal]) -> list[RankedPick]:
        """Order ``candidates`` best-first, by model when one is usable.

        An empty result means one of two things and the difference matters:
        the model read the book and would take none of it (respected — the
        room stays quiet), or no model answered at all (fallen back on, so a
        provider outage does not silently empty a premium room). Only the
        second reaches the heuristic.
        """
        if not candidates:
            return []
        picks = self._rank_with_ai(candidates) if self.ai_available else None
        if picks is not None:
            return picks
        return [
            RankedPick(signal=sig, rank=i + 1, source="heuristic")
            for i, sig in enumerate(candidates[:self._max_picks])
        ]

    def _rank_with_ai(self, candidates: Sequence[Signal]) -> Optional[list[RankedPick]]:
        """The model's ranking, or ``None`` when it did not produce one."""
        by_id = {sig.id: sig for sig in candidates}
        book = "\n\n".join(_facts(sig, age_minutes(sig.created_at)) for sig in candidates)
        prompt = (
            f"LIVE SETUPS ({len(candidates)}). The order below is not a ranking.\n\n"
            f"{book}\n\n"
            f"Return at most {self._max_picks} picks, best first, using the ids "
            f"exactly as given. Omit any setup you would not take."
        )
        try:
            data = self._llm.complete_json(
                _RANKER_SYSTEM, prompt, _RANKING_SCHEMA, max_tokens=self._max_tokens
            )
        except Exception:  # the AI layer must never break the report
            log.exception("Conviction ranking call failed — falling back to heuristic order")
            return None
        raw = (data or {}).get("picks")
        if not isinstance(raw, list):
            # The one failure that looks like success: a 200 carrying nothing
            # usable. Named here because the card would otherwise present the
            # heuristic order with no sign the model had been asked at all.
            log.warning(
                "Ranker returned no usable picks (%s) — falling back to heuristic order",
                getattr(self._llm, "last_error", "") or "no picks array",
            )
            return None

        out: list[RankedPick] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            sig = by_id.get(str(entry.get("id", "")).strip())
            if sig is None or sig.id in seen:
                # A hallucinated or repeated id is dropped rather than
                # guessed at: the alternative is a card recommending a trade
                # that does not exist.
                log.debug("Ranker returned an unusable id %r — dropped", entry.get("id"))
                continue
            try:
                conviction = max(0, min(100, int(entry.get("conviction", 0))))
            except (TypeError, ValueError):
                conviction = 0
            if conviction < self._min_conviction:
                continue
            seen.add(sig.id)
            out.append(RankedPick(
                signal=sig,
                rank=len(out) + 1,
                conviction=conviction,
                thesis=str(entry.get("thesis", "")).strip()[:240],
                risk=str(entry.get("risk", "")).strip()[:240],
                source="ai",
            ))
            if len(out) >= self._max_picks:
                break
        return out

    # ── de-duplication ────────────────────────────────────────────────
    def _already_posted(self, picks: Sequence[RankedPick]) -> bool:
        """Whether this exact ranking is the one already on the board.

        Ordered comparison, so a re-shuffle of the same three setups is news and
        is posted. Without a store the check is skipped rather than faked —
        every ranking posts, which is the old behaviour of every other report.
        """
        if self._store is None:
            return False
        ids = [p.signal.id for p in picks]
        previous = self._store.read(STATE_KEY, default=None) or {}
        return list(previous.get("ids") or []) == ids

    def _remember(self, picks: Sequence[RankedPick]) -> None:
        if self._store is None:
            return
        self._store.write(STATE_KEY, {
            "ids": [p.signal.id for p in picks],
            "posted_at": datetime.now(timezone.utc).isoformat(),
        })

    # ── rendering ─────────────────────────────────────────────────────
    def build(self, force: bool = False, remember: bool = True) -> Optional[str]:
        """Render the ranking card, or ``None`` when there is nothing to say.

        ``force`` skips only the unchanged-ranking check, for a ranking someone
        asked for by hand: the scheduled job stays quiet when nothing moved,
        but a person typing ``/rank`` is owed an answer even if it is the same
        one. The "too few setups" and "the AI would take none" cases still
        return ``None`` — there is genuinely nothing to render.

        ``remember`` records the result as what is now on the High-Conviction
        board, and must be false for a caller that does not post it there.
        A ``/rank`` reply that lands in another chat would otherwise suppress
        the next scheduled post of a ranking the room never saw.
        """
        candidates = self.candidates()
        if len(candidates) < self._min_candidates:
            log.debug(
                "Conviction ranking: %d live setup(s), need %d — skipping",
                len(candidates), self._min_candidates,
            )
            return None
        picks = self.rank(candidates)
        if not picks:
            log.info(
                "Conviction ranking: the AI would take none of the %d live setups",
                len(candidates),
            )
            return None
        if not force and self._already_posted(picks):
            log.debug("Conviction ranking unchanged since the last post — skipping")
            return None
        if remember:
            self._remember(picks)
        return self._card(picks, candidates)

    def _card(self, picks: Sequence[RankedPick], candidates: Sequence[Signal]) -> str:
        heuristic = picks[0].source == "heuristic"
        header = (
            f"⚠️ AI unavailable — ordered by signal score ({len(candidates)} live setups)"
            if heuristic else
            f"🧠 AI ranked {len(candidates)} live setups · top {len(picks)}"
        )
        blocks = [
            f"🏆 <b>HIGH-CONVICTION RANKING</b>\n{DIVIDER}\n{header}",
            *[self._pick_block(p) for p in picks],
        ]
        passed = [s for s in candidates if s.id not in {p.signal.id for p in picks}]
        if passed:
            blocks.append(self._passed_block(passed))
        return f"\n{DIVIDER}\n".join(blocks) + f"\n{self._stamp()}"

    def _pick_block(self, pick: RankedPick) -> str:
        sig = pick.signal
        badge = _BADGES[pick.rank - 1] if pick.rank <= len(_BADGES) else f"#{pick.rank}"
        arrow = "🟢" if sig.is_long else "🔴"
        grade = (f"conviction {pick.conviction}%" if pick.source == "ai"
                 else f"score {sig.score}/100")
        age = age_minutes(sig.created_at)
        age_str = f" · {age:.0f}m ago" if age is not None else ""
        lines = [
            f"{badge} {arrow} <b>{esc(sig.symbol)}</b> · {esc(sig.direction)} · {esc(grade)}",
            f"⚡ {esc(sig.strategy)} · {esc(sig.timeframe)} · {esc(sig.status)}{esc(age_str)}",
            f"💵 <code>{fmt_price(sig.entry_price)}</code> → "
            f"🎯 <code>{fmt_price(final_target(sig))}</code>"
            f" · 🛑 <code>{fmt_price(sig.sl)}</code> · R:R {rr_of(sig):.1f}",
        ]
        if pick.thesis:
            lines.append(f"💡 {esc(pick.thesis)}")
        if pick.risk:
            lines.append(f"⚠️ {esc(pick.risk)}")
        return "\n".join(lines)

    def _passed_block(self, passed: Sequence[Signal]) -> str:
        """Name what was on the board and did not make the cut.

        A leaderboard that only shows winners cannot be checked. Saying which
        setups were considered and passed over is what makes the ranking
        falsifiable later, when those trades resolve.
        """
        shown = [f"{esc(s.symbol)} {esc(s.direction)}" for s in passed[:6]]
        more = f" +{len(passed) - 6} more" if len(passed) > 6 else ""
        return f"⬜ Considered, not picked: {', '.join(shown)}{more}"

    def _stamp(self) -> str:
        return f"🕐 {now(self._tz)}"
