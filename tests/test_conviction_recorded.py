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
    assert _conviction_label(sig) == "SCORE_PICK"


def test_the_diag_separates_picks_from_what_the_room_passed_over():
    def s(**kw):
        return Signal(symbol="X", signal_type="S", direction="LONG",
                      entry_price=100, tp=110, sl=95, **kw)

    assert _conviction_label(s(conviction_rank=1, conviction_source="ai")) == "AI_PICK"
    assert _conviction_label(s(conviction_considered=True)) == "PASSED"
    assert _conviction_label(s()) == "UNRANKED"


def test_a_failure_to_record_never_costs_the_room_its_card(store, fake_client):
    """The card is what the room exists for; a lost label must not take it."""
    tracker = Tracker(store, fake_client, TrackerSettings())
    a, b = _book(tracker, "AAAUSDT", "BBBUSDT")

    def boom(*_a, **_k):
        raise RuntimeError("state write failed")

    tracker.mark_conviction = boom
    assert _ranker(tracker, store, [(a.id, 80)]).build()
