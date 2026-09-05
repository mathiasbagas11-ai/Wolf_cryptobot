"""Tests for the AI conviction ranking of the live signal book."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.config import TelegramSettings
from wolf.models import Signal, Status
from wolf.reports import ConvictionRanker
from wolf.reports.conviction import STATE_KEY, final_target, heuristic_score, rr_of


class FakeLLM:
    """Returns a canned ranking and records what it was asked."""

    def __init__(self, payload=None, available=True):
        self.payload = payload if payload is not None else {"picks": []}
        self._available = available
        self.prompts: list[str] = []
        self.last_error = ""

    @property
    def available(self) -> bool:
        return self._available

    def complete(self, system, user, *, max_tokens=1024):
        return ""

    def complete_json(self, system, user, schema, *, max_tokens=1024):
        self.prompts.append(user)
        return self.payload


class FakeTracker:
    def __init__(self, signals):
        self._signals = list(signals)

    def active_signals(self):
        return list(self._signals)


def _signal(symbol="BTCUSDT", **kw) -> Signal:
    base = dict(
        symbol=symbol, signal_type="PREPUMP", direction="LONG",
        entry_price=100.0, tp=110.0, sl=95.0, score=70,
        confluence_level="MEDIUM", strategy="PREPUMP", timeframe="1h",
        reasons=["volume coil"],
    )
    base.update(kw)
    sig = Signal(**base)
    sig.id = f"{symbol}_id"
    return sig


def _ranker(signals, llm=None, store=None, **kw):
    opts = dict(min_candidates=2, max_picks=3, min_conviction=60)
    opts.update(kw)
    return ConvictionRanker(FakeTracker(signals), store, llm=llm, **opts)


def _pick(sig, conviction=80, thesis="clean structure", risk="loses the level"):
    return {"id": sig.id, "conviction": conviction, "thesis": thesis, "risk": risk}


# ── candidate selection ────────────────────────────────────────────────────
def test_only_live_signals_within_the_lookback_are_ranked():
    fresh = _signal("BTCUSDT")
    stale = _signal("ETHUSDT", created_at=(
        datetime.now(timezone.utc) - timedelta(hours=20)).isoformat())
    resolved = _signal("SOLUSDT", status=Status.TP_HIT.value)
    ranker = _ranker([fresh, stale, resolved], lookback_hours=12.0)
    assert [s.symbol for s in ranker.candidates()] == ["BTCUSDT"]


def test_unreadable_timestamp_is_kept_not_dropped():
    """The tracker still calls it live; a bad clock is not a reason to hide it."""
    sig = _signal("BTCUSDT", created_at="not-a-date")
    assert _ranker([sig]).candidates() == [sig]


def test_no_card_when_there_is_nothing_to_compare():
    ranker = _ranker([_signal("BTCUSDT")], llm=FakeLLM())
    assert ranker.build() is None


# ── AI ranking ─────────────────────────────────────────────────────────────
def test_ai_order_is_the_card_order():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT", score=95)
    llm = FakeLLM({"picks": [_pick(eth, 90), _pick(btc, 75)]})
    picks = _ranker([btc, eth], llm=llm).rank([btc, eth])
    assert [p.signal.symbol for p in picks] == ["ETHUSDT", "BTCUSDT"]
    assert [p.rank for p in picks] == [1, 2]
    assert all(p.source == "ai" for p in picks)


def test_every_live_setup_reaches_the_prompt():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc)]})
    _ranker([btc, eth], llm=llm).build()
    prompt = llm.prompts[0]
    assert btc.id in prompt and eth.id in prompt


def test_hallucinated_id_is_dropped():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [{"id": "DOGEUSDT_id", "conviction": 99,
                              "thesis": "t", "risk": "r"}, _pick(btc)]})
    picks = _ranker([btc, eth], llm=llm).rank([btc, eth])
    assert [p.signal.symbol for p in picks] == ["BTCUSDT"]


def test_repeated_id_is_counted_once():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc), _pick(btc)]})
    picks = _ranker([btc, eth], llm=llm).rank([btc, eth])
    assert len(picks) == 1


def test_low_conviction_picks_are_dropped_not_ranked_last():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc, 80), _pick(eth, 40)]})
    picks = _ranker([btc, eth], llm=llm, min_conviction=60).rank([btc, eth])
    assert [p.signal.symbol for p in picks] == ["BTCUSDT"]


def test_max_picks_caps_the_leaderboard():
    signals = [_signal(f"S{i}USDT") for i in range(5)]
    llm = FakeLLM({"picks": [_pick(s) for s in signals]})
    assert len(_ranker(signals, llm=llm, max_picks=2).rank(signals)) == 2


def test_no_card_when_the_ai_would_take_nothing():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    assert _ranker([btc, eth], llm=FakeLLM({"picks": []})).build() is None


def test_an_unreadable_answer_falls_back_to_heuristic_order():
    """No picks array at all is a broken answer, not a verdict of "take none"."""
    strong = _signal("BTCUSDT", score=90)
    weak = _signal("ETHUSDT", score=40)
    picks = _ranker([strong, weak], llm=FakeLLM({})).rank([strong, weak])
    assert [p.signal.symbol for p in picks] == ["BTCUSDT", "ETHUSDT"]
    assert all(p.source == "heuristic" for p in picks)


def test_ai_exception_falls_back_to_heuristic_order():
    class Exploding(FakeLLM):
        def complete_json(self, system, user, schema, *, max_tokens=1024):
            raise RuntimeError("provider down")

    strong = _signal("BTCUSDT", score=90)
    weak = _signal("ETHUSDT", score=40)
    picks = _ranker([strong, weak], llm=Exploding()).rank([strong, weak])
    assert [p.signal.symbol for p in picks] == ["BTCUSDT", "ETHUSDT"]
    assert all(p.source == "heuristic" for p in picks)


# ── heuristic fallback ─────────────────────────────────────────────────────
def test_no_llm_ranks_by_heuristic_and_says_so_on_the_card():
    strong = _signal("BTCUSDT", score=90)
    weak = _signal("ETHUSDT", score=40)
    card = _ranker([strong, weak]).build()
    assert "AI unavailable" in card
    assert "score 90/100" in card       # never presented as a conviction
    assert "conviction" not in card


def test_heuristic_rewards_a_confirmed_setup_and_punishes_a_flagged_one():
    plain = _signal("BTCUSDT")
    confirmed = _signal("ETHUSDT", ai_verdict="CONFIRM", ai_confidence=90)
    flagged = _signal("SOLUSDT", against_regime=True, weak_strategy=True)
    assert heuristic_score(confirmed) > heuristic_score(plain) > heuristic_score(flagged)


def test_rr_measures_to_the_furthest_ladder_rung():
    sig = _signal(tp=110.0, sl=95.0,
                  tp_ladder=[{"level": 1, "price": 105.0}, {"level": 2, "price": 125.0}])
    assert rr_of(sig) == 5.0            # (125 - 100) / (100 - 95)


def test_the_card_quotes_the_target_its_ratio_is_measured_to():
    btc = _signal("BTCUSDT", tp=110.0,
                  tp_ladder=[{"level": 1, "price": 105.0}, {"level": 2, "price": 125.0}])
    eth = _signal("ETHUSDT")
    card = _ranker([btc, eth], llm=FakeLLM({"picks": [_pick(btc)]})).build()
    assert final_target(btc) == 125.0
    assert "125.0000" in card and "R:R 5.0" in card


def test_rr_of_a_stop_at_the_entry_is_zero_not_a_crash():
    assert rr_of(_signal(entry_price=100.0, sl=100.0)) == 0.0


# ── card ───────────────────────────────────────────────────────────────────
def test_card_names_the_picks_and_what_was_passed_over():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc, 88, "reclaimed the range high",
                                   "back inside the range")]})
    card = _ranker([btc, eth], llm=llm).build()
    assert "HIGH-CONVICTION RANKING" in card
    assert "🥇" in card and "BTCUSDT" in card
    assert "conviction 88%" in card
    assert "reclaimed the range high" in card
    assert "back inside the range" in card
    assert "Considered, not picked" in card and "ETHUSDT" in card


def test_card_escapes_model_text():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc, 88, "<b>hype</b>", "r")]})
    card = _ranker([btc, eth], llm=llm).build()
    assert "<b>hype</b>" not in card and "&lt;b&gt;hype&lt;/b&gt;" in card


# ── de-duplication ─────────────────────────────────────────────────────────
def test_the_same_ranking_is_not_posted_twice(store):
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc)]})
    ranker = _ranker([btc, eth], llm=llm, store=store)
    assert ranker.build() is not None
    assert ranker.build() is None
    assert store.read(STATE_KEY)["ids"] == [btc.id]


def test_a_reshuffle_of_the_same_setups_is_news(store):
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    llm = FakeLLM({"picks": [_pick(btc), _pick(eth)]})
    ranker = _ranker([btc, eth], llm=llm, store=store)
    assert ranker.build() is not None
    llm.payload = {"picks": [_pick(eth), _pick(btc)]}
    assert ranker.build() is not None


def test_force_answers_an_unchanged_ranking(store):
    """A person who typed /rank is owed an answer, repeat or not."""
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    ranker = _ranker([btc, eth], llm=FakeLLM({"picks": [_pick(btc)]}), store=store)
    assert ranker.build() is not None
    assert ranker.build() is None
    assert ranker.build(force=True) is not None


def test_an_unremembered_ranking_does_not_suppress_the_next_post(store):
    """/rank replies in the chat it was asked from; the room never saw it."""
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    ranker = _ranker([btc, eth], llm=FakeLLM({"picks": [_pick(btc)]}), store=store)
    assert ranker.build(force=True, remember=False) is not None
    assert ranker.build() is not None
    assert store.read(STATE_KEY)["ids"] == [btc.id]


def test_force_does_not_manufacture_a_ranking_out_of_nothing(store):
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    ranker = _ranker([btc, eth], llm=FakeLLM({"picks": []}), store=store)
    assert ranker.build(force=True) is None


def test_without_a_store_every_ranking_posts():
    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    ranker = _ranker([btc, eth], llm=FakeLLM({"picks": [_pick(btc)]}))
    assert ranker.build() is not None
    assert ranker.build() is not None


# ── routing ────────────────────────────────────────────────────────────────
def test_ranking_routes_to_the_high_conviction_topic():
    s = TelegramSettings(bot_token="t", chat_id="1",
                         high_conviction_thread_id="99", new_signal_thread_id="1")
    assert s.route_conviction() == "99"


def test_ranking_falls_back_to_new_signal_not_the_main_channel():
    s = TelegramSettings(bot_token="t", chat_id="1", new_signal_thread_id="1")
    assert s.route_conviction() == "1"


# ── /rank command ──────────────────────────────────────────────────────────
def test_rank_command_answers_in_the_room_it_was_asked_from():
    from types import SimpleNamespace

    from wolf.notify.commands import CommandRouter

    btc, eth = _signal("BTCUSDT"), _signal("ETHUSDT")
    ranker = _ranker([btc, eth], llm=FakeLLM({"picks": [_pick(btc)]}))
    reply = CommandRouter(SimpleNamespace(conviction=ranker)).handle("/rank")
    assert "HIGH-CONVICTION RANKING" in reply and "BTCUSDT" in reply


def test_rank_command_says_when_the_feature_is_off():
    from types import SimpleNamespace

    from wolf.notify.commands import CommandRouter

    reply = CommandRouter(SimpleNamespace(conviction=None, analyze=None)).handle("/rank")
    assert "CONVICTION_RANKING_ENABLED" in reply


def test_rank_command_says_when_there_is_nothing_to_rank():
    from types import SimpleNamespace

    from wolf.notify.commands import CommandRouter

    ranker = _ranker([_signal("BTCUSDT")], llm=FakeLLM({"picks": []}))
    reply = CommandRouter(SimpleNamespace(conviction=ranker)).handle("/rank")
    assert "Nothing to rank" in reply
