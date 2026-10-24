"""The High-Conviction pick travels with the trade into the ledger.

The room ranks signals that are already live, so every pick was always an
ordinary signal in the outcome log — but the pick itself lived in one
overwritten "last posted" key. The Trade Report closing the trade could not
say it had been recommended, and the diag could not ask whether a 🥇 beat the
rest. These tests run the real tracker, because the whole point is the
pairing: a ranker that stamps a fake is proof of nothing.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from wolf.config import TelegramSettings, TrackerSettings
from wolf.diagnose import _conviction_label
from wolf.models import Candle, Signal, Status
from wolf.notify import TelegramNotifier
from wolf.reports import ConvictionRanker
from wolf.tracker import OUTCOMES_KEY, Tracker

_LADDER = [
    {"level": 1, "price": 105, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 110, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 115, "allocation": 0.2, "r_multiple": 3.0},
]


class _LLM:
    available = True
    last_error = ""

    def __init__(self, order):
        self.order = order

    def complete_json(self, system, user, schema, *, max_tokens=1024):
        return {"picks": [{"id": i, "conviction": c, "thesis": "t", "risk": "r"}
                          for i, c in self.order]}


def _book(tracker, *symbols):
    return [tracker.record_signal(sym, "SCREENER", "LONG", 100, tp=115, sl=95,
                                  entry_mode="MOMENTUM_NOW", tps=_LADDER)
            for sym in symbols]


def _ranker(tracker, store, order):
    return ConvictionRanker(tracker, store, llm=_LLM(order),
                            min_candidates=2, max_picks=3, min_conviction=60)


def _live(tracker):
    return {s.symbol: s for s in tracker.active_signals()}


def test_a_posted_ranking_is_written_onto_the_signals_it_ranked(store, fake_client):
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b, c = _book(tracker, "AAAUSDT", "BBBUSDT", "CCCUSDT")

    assert _ranker(tracker, store, [(b.id, 82), (a.id, 70)]).build()

    live = _live(tracker)
    assert (live["BBBUSDT"].conviction_rank, live["BBBUSDT"].conviction_score,
            live["BBBUSDT"].conviction_source) == (1, 82, "ai")
    assert live["AAAUSDT"].conviction_rank == 2
    # Passed over from the same book at the same moment — the comparison
    # that means something, recorded rather than inferred.
    assert live["CCCUSDT"].conviction_rank == 0
    assert live["CCCUSDT"].conviction_considered is True


def test_the_best_rank_a_signal_reached_is_the_one_kept(store, fake_client):
    """Once the room called it the top pick, a reshuffle does not demote it."""
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b = _book(tracker, "AAAUSDT", "BBBUSDT")

    _ranker(tracker, store, [(a.id, 70), (b.id, 65)]).build()
    _ranker(tracker, store, [(b.id, 90), (a.id, 80)]).build()

    live = _live(tracker)
    assert (live["AAAUSDT"].conviction_rank, live["AAAUSDT"].conviction_score) == (1, 70)
    assert (live["BBBUSDT"].conviction_rank, live["BBBUSDT"].conviction_score) == (1, 90)


def test_a_ranking_nobody_posted_to_the_room_records_nothing(store, fake_client):
    """/rank answers in whatever chat asked. That is not the room
    recommending anything, so it is not recorded as if it were."""
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b = _book(tracker, "AAAUSDT", "BBBUSDT")

    assert _ranker(tracker, store, [(a.id, 80)]).build(force=True, remember=False)
    assert all(s.conviction_rank == 0 and not s.conviction_considered
               for s in tracker.active_signals())


def test_the_pick_reaches_the_trade_report_that_closes_the_trade(store, fake_client):
    """The owner's complaint, as a test: the two records were separate."""
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, _ = _book(tracker, "AAAUSDT", "BBBUSDT")
    _ranker(tracker, store, [(a.id, 82)]).build()

    created = [s for s in tracker.active_signals() if s.symbol == "AAAUSDT"][0].created_at
    t0 = int(datetime.fromisoformat(created).timestamp() * 1000)
    fake_client.klines["AAAUSDT"] = [
        Candle(time=t0 + (i + 1) * 900_000, open=o, high=h, low=l, close=c, volume=1.0)
        for i, (o, h, l, c) in enumerate([(100, 116, 99, 115)])
    ]
    closed = [s for s in tracker.check_pending() if s.symbol == "AAAUSDT"][0]
    assert closed.conviction_rank == 1

    # It survives into the outcome log, which is what the diag reads.
    row = [d for d in store.read(OUTCOMES_KEY) if d["symbol"] == "AAAUSDT"][0]
    assert row["conviction_rank"] == 1 and row["conviction_source"] == "ai"

    text = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1"))._resolved_text(closed)
    assert "High Conviction #1" in text and "AI 82%" in text


def test_a_trade_the_room_never_picked_carries_no_badge():
    """Absence is not printed: a report that said "not a pick" on every
    ordinary trade would bury the one line worth reading."""
    sig = Signal(symbol="X", signal_type="S", direction="LONG", entry_price=100,
                 tp=110, sl=95, status=Status.TP_HIT.value, exit_price=110, pnl_pct=10)
    text = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1"))._resolved_text(sig)
    assert "High Conviction" not in text


def test_a_score_ordered_pick_says_the_ai_did_not_make_it():
    """The room falls back to sorting by score when the model is down. That
    is not a verdict, and a badge that let it pass for one would be the exact
    misrepresentation the room was written to avoid."""
    sig = Signal(symbol="X", signal_type="S", direction="LONG", entry_price=100,
                 tp=110, sl=95, status=Status.TP_HIT.value, exit_price=110, pnl_pct=10,
                 conviction_rank=2, conviction_source="heuristic")
    text = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1"))._resolved_text(sig)
    assert "AI was unavailable" in text and "AI 0%" not in text
    sig.conviction_first_rank, sig.conviction_first_source = 2, "heuristic"
    assert _conviction_label(sig) == "SCORE_ORDERED"


def _s(**kw):
    return Signal(symbol="X", signal_type="S", direction="LONG",
                  entry_price=100, tp=110, sl=95, **kw)


def test_the_diag_reads_the_first_look_not_the_best_rank():
    """A signal passed over at first look and picked five hours later is a
    pass. Counting the later pick is how a winner that simply lived longer
    gets credited to the room's judgement."""
    assert _conviction_label(_s(conviction_first_rank=1, conviction_first_source="ai")) == "AI_PICK"
    assert _conviction_label(_s(conviction_first_rank=0, conviction_first_source="ai",
                                conviction_rank=1, conviction_source="ai")) == "PASSED"
    assert _conviction_label(_s()) == "UNRANKED"


def test_rows_considered_before_first_looks_were_recorded_stay_unranked():
    """They were labelled under the biased rule. Folding them into either
    side would pool two definitions under one name."""
    assert _conviction_label(_s(conviction_considered=True, conviction_rank=1,
                                conviction_source="ai")) == "UNRANKED"


def test_the_first_look_is_recorded_once_and_never_revised(store, fake_client):
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b, c = _book(tracker, "AAAUSDT", "BBBUSDT", "CCCUSDT")

    _ranker(tracker, store, [(a.id, 80)]).build()             # first look: A picked
    _ranker(tracker, store, [(b.id, 90), (c.id, 85)]).build()  # B, C picked later

    live = _live(tracker)
    assert live["AAAUSDT"].conviction_first_rank == 1
    assert live["BBBUSDT"].conviction_first_rank == 0     # passed first, picked later
    assert live["CCCUSDT"].conviction_first_rank == 0
    assert live["BBBUSDT"].conviction_rank == 1            # the badge still knows
    assert _conviction_label(live["BBBUSDT"]) == "PASSED"


def test_a_newcomer_gets_its_first_look_even_when_the_ranking_is_unchanged(store, fake_client):
    """An unchanged ranking is not posted, but the room still judged the book.
    Waiting for the picks to change before recording the newcomer's first look
    would push it later, and later is the bias."""
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b = _book(tracker, "AAAUSDT", "BBBUSDT")
    _ranker(tracker, store, [(a.id, 80)]).build()

    (c,) = _book(tracker, "CCCUSDT")
    assert _ranker(tracker, store, [(a.id, 80)]).build() is None   # not re-posted
    live = _live(tracker)
    assert live["CCCUSDT"].conviction_first_rank == 0
    assert live["CCCUSDT"].conviction_first_source == "ai"


def test_a_room_that_would_take_nothing_passed_over_everything(store, fake_client):
    tracker = Tracker(store, fake_client, TrackerSettings())
    _book(tracker, "AAAUSDT", "BBBUSDT")

    assert _ranker(tracker, store, []).build() is None
    assert all(s.conviction_first_rank == 0 and s.conviction_first_source == "ai"
               for s in tracker.active_signals())


def test_a_zero_skill_picker_shows_no_gap_at_first_look():
    """The reason this bucket reads first looks, run as a check rather than
    asserted in a comment. A picker that ignores the outcome, looking once an
    hour at signals whose losers die fast and winners live long, manufactures
    a large gap under "ever picked" and none at first look."""
    import random
    import statistics

    rng = random.Random(7)
    ever, first = {True: [], False: []}, {True: [], False: []}
    for _ in range(4000):
        win = rng.random() < 0.5
        r = rng.choice([0.5, 1.0, 1.7]) if win else -1.0
        looks = max(1, int(rng.uniform(6, 48) if win else rng.uniform(1, 6)))
        picks = [rng.random() < 0.4 for _ in range(looks)]
        ever[any(picks)].append(r)
        first[picks[0]].append(r)

    def gap(d):
        return statistics.fmean(d[True]) - statistics.fmean(d[False])

    assert gap(ever) > 0.8          # a coin, reading as a skilled room
    assert abs(gap(first)) < 0.1    # a coin, reading as a coin


def test_a_failure_to_record_never_costs_the_room_its_card(store, fake_client):
    """The card is what the room exists for; a lost label must not take it."""
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b = _book(tracker, "AAAUSDT", "BBBUSDT")

    def boom(*_a, **_k):
        raise RuntimeError("state write failed")

    tracker.mark_conviction = boom
    assert _ranker(tracker, store, [(a.id, 80)]).build()
