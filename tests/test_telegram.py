"""Tests for the Telegram notifier: routing, formatting, error handling."""

from __future__ import annotations

from wolf.config import TelegramSettings
from wolf.models import Signal
from wolf.notify import TelegramNotifier


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"ok": True}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Captures sendMessage payloads instead of hitting the network."""

    def __init__(self, status_code=200, body=None):
        self.calls: list[dict] = []
        self._status = status_code
        self._body = body

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return FakeResponse(self._status, self._body)


def _settings(**kw) -> TelegramSettings:
    base = dict(bot_token="t", chat_id="123")
    base.update(kw)
    return TelegramSettings(**base)


def _signal(**kw) -> Signal:
    base = dict(
        symbol="BTCUSDT", signal_type="PREPUMP", direction="LONG",
        entry_price=65000, tp=68000, sl=63500, score=78, confluence_level="HIGH",
        reasons=["Bollinger squeeze", "Volume coil 2.1x"], strategy="PREPUMP",
        entry_mode="MOMENTUM_NOW",
        tp_ladder=[{"level": 1, "price": 66500}, {"level": 2, "price": 68000}],
    )
    base.update(kw)
    return Signal(**base)


# ── routing ────────────────────────────────────────────────────────────────
def test_each_topic_routes_to_its_own_thread():
    s = _settings(
        new_signal_thread_id="1", signal_thread_id="2", market_update_thread_id="3",
        trade_report_thread_id="4", news_thread_id="5", whale_thread_id="6",
        radar_thread_id="7", majors_thread_id="8",
    )
    assert s.route_new_signal() == "1"
    assert s.route_entry() == "2"           # Signal Entry
    assert s.route_market_update() == "3"
    assert s.route_trade_report() == "4"
    assert s.route_news() == "5"
    assert s.route_whale() == "6"
    assert s.route_radar() == "7"
    assert s.route_majors() == "8"


def test_unconfigured_topic_falls_back_to_main():
    assert _settings().route_new_signal() == ""
    assert _settings().route_majors() == ""


def test_route_stats_falls_back_to_system():
    assert _settings(system_thread_id="5").route_stats() == "5"
    assert _settings(stats_thread_id="7", system_thread_id="5").route_stats() == "7"


# ── high-conviction (TRAP) topic routing ───────────────────────────────────
def test_trap_announce_routes_to_high_conviction_topic():
    sess = FakeSession()
    n = TelegramNotifier(_settings(new_signal_thread_id="1", high_conviction_thread_id="99"), session=sess)
    n.announce_signal(_signal(signal_type="TRAP", strategy="TRAP"))
    assert sess.calls[0]["message_thread_id"] == "99"


def test_trap_lifecycle_routes_to_high_conviction_topic():
    sess = FakeSession()
    n = TelegramNotifier(
        _settings(signal_thread_id="2", trade_report_thread_id="4", high_conviction_thread_id="99"),
        session=sess,
    )
    sig = _signal(signal_type="TRAP", strategy="TRAP")
    n.on_event(sig, "ACTIVATED", {})
    n.on_event(sig, "RESOLVED", {})
    assert sess.calls[0]["message_thread_id"] == "99"  # entry, not "2"
    assert sess.calls[1]["message_thread_id"] == "99"  # resolution, not "4"


def test_trap_falls_back_to_normal_topics_when_unconfigured():
    sess = FakeSession()
    n = TelegramNotifier(_settings(new_signal_thread_id="1", trade_report_thread_id="4"), session=sess)
    sig = _signal(signal_type="TRAP", strategy="TRAP")
    n.announce_signal(sig)
    n.on_event(sig, "RESOLVED", {})
    assert sess.calls[0]["message_thread_id"] == "1"  # New Signal
    assert sess.calls[1]["message_thread_id"] == "4"  # Trade Reports


def test_non_trap_ignores_high_conviction_topic():
    sess = FakeSession()
    n = TelegramNotifier(_settings(new_signal_thread_id="1", high_conviction_thread_id="99"), session=sess)
    n.announce_signal(_signal(signal_type="PREPUMP", strategy="PREPUMP"))
    assert sess.calls[0]["message_thread_id"] == "1"


# ── risk-flag rendering ─────────────────────────────────────────────────────
def test_signal_card_shows_risk_flags():
    sess = FakeSession()
    n = TelegramNotifier(_settings(), session=sess)
    n.announce_signal(_signal(against_regime=True, weak_strategy=True))
    text = sess.calls[0]["text"]
    assert "against-regime" in text
    assert "weak-strategy" in text
    assert "monitor" in text


def test_signal_card_no_risk_line_when_unflagged():
    sess = FakeSession()
    n = TelegramNotifier(_settings(), session=sess)
    n.announce_signal(_signal())
    assert "Risk:" not in sess.calls[0]["text"]


def test_stats_card_risk_gate_monitor():
    sess = FakeSession()
    n = TelegramNotifier(_settings(), session=sess)
    n.notify_stats({
        "wins": 10, "losses": 10, "win_rate": 50.0, "avg_pnl_pct": 0.5, "active": 1,
        "by_strategy": {}, "by_ai_verdict": {}, "vetoed_count": 0, "vetoed_win_rate": None,
        "against_regime_count": 12, "against_regime_win_rate": 25.0,  # -25pp vs avg
        "weak_flag_count": 8, "weak_flag_win_rate": 60.0,             # +10pp vs avg
    })
    text = sess.calls[0]["text"]
    assert "Risk-gate monitor" in text
    assert "Against-regime" in text and "enable REGIME_HARD_BLOCK" in text
    assert "Weak-strategy" in text and "not hurting" in text


# ── disabled notifier is a no-op ───────────────────────────────────────────
def test_disabled_notifier_sends_nothing():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(), session=sess)  # no token/chat
    assert n.send("hi") is False
    assert sess.calls == []


# ── startup + routing ──────────────────────────────────────────────────────
def test_startup_goes_to_system_thread():
    sess = FakeSession()
    n = TelegramNotifier(_settings(system_thread_id="42"), session=sess)
    n.notify_startup({"sources": ["binance", "okx"], "detectors": ["MOMENTUM"],
                      "universe": 15, "scan_min": 10, "track_min": 5, "ai": False})
    assert len(sess.calls) == 1
    assert sess.calls[0]["message_thread_id"] == "42"
    assert "ONLINE" in sess.calls[0]["text"]
    assert "binance → okx" in sess.calls[0]["text"]


def test_announce_signal_card_content_and_route():
    sess = FakeSession()
    n = TelegramNotifier(_settings(new_signal_thread_id="11"), session=sess)
    n.announce_signal(_signal())
    payload = sess.calls[0]
    assert payload["message_thread_id"] == "11"
    text = payload["text"]
    assert "NEW SIGNAL · PREPUMP" in text
    assert "BTCUSDT" in text and "LONG" in text
    assert "TP1" in text and "TP2" in text
    assert "R:R" in text
    assert "Bollinger squeeze" in text


def test_on_event_routing():
    sess = FakeSession()
    n = TelegramNotifier(
        _settings(signal_thread_id="20", trade_report_thread_id="30"),
        session=sess,
    )
    sig = _signal(status="ACTIVE")
    n.on_event(sig, "ACTIVATED", {"price": 65000})           # -> Signal Entry (20)
    n.on_event(sig, "TP_HIT", {"level": 1, "price": 66500})  # -> Signal Entry (20)
    resolved = _signal(status="TP_HIT", pnl_pct=4.6, hold_hours=3.2)
    n.on_event(resolved, "RESOLVED", {})                     # -> Trade Reports (30)
    threads = [c["message_thread_id"] for c in sess.calls]
    assert threads == ["20", "20", "30"]
    assert "ENTRY TOUCHED" in sess.calls[0]["text"]
    assert "TP1 HIT" in sess.calls[1]["text"]
    assert "WIN" in sess.calls[2]["text"] and "+4.60%" in sess.calls[2]["text"]


def test_report_notify_routing():
    sess = FakeSession()
    n = TelegramNotifier(
        _settings(majors_thread_id="80", radar_thread_id="70",
                  market_update_thread_id="30", whale_thread_id="60"),
        session=sess,
    )
    n.notify_majors("majors card")
    n.notify_radar("radar card")
    n.notify_pulse("pulse card")
    n.notify_whale("whale card")
    threads = [c["message_thread_id"] for c in sess.calls]
    assert threads == ["80", "70", "30", "60"]
    # Empty text is a no-op.
    n.notify_majors(None)
    assert len(sess.calls) == 4


def test_resolved_loss_formatting():
    sess = FakeSession()
    n = TelegramNotifier(_settings(), session=sess)
    n.on_event(_signal(status="SL_HIT", pnl_pct=-2.3, hold_hours=1.1), "RESOLVED", {})
    assert "LOSS" in sess.calls[0]["text"] and "-2.30%" in sess.calls[0]["text"]


def test_stats_card():
    sess = FakeSession()
    n = TelegramNotifier(_settings(stats_thread_id="9"), session=sess)
    n.notify_stats({
        "wins": 12, "losses": 8, "win_rate": 60.0, "avg_pnl_pct": 1.8, "active": 3,
        "by_strategy": {"MOMENTUM": {"win_rate": 65.0, "total": 20, "avg_pnl": 1.2}},
        "by_ai_verdict": {},
        "vetoed_count": 0, "vetoed_win_rate": None,
    })
    text = sess.calls[0]["text"]
    assert sess.calls[0]["message_thread_id"] == "9"
    assert "PERFORMANCE SUMMARY" in text
    assert "Win rate 60.0%" in text
    assert "MOMENTUM" in text
    # No AI section when no AI data
    assert "AI verdict accuracy" not in text


def test_stats_card_ai_section():
    sess = FakeSession()
    n = TelegramNotifier(_settings(), session=sess)
    n.notify_stats({
        "wins": 10, "losses": 10, "win_rate": 50.0, "avg_pnl_pct": 0.5, "active": 2,
        "by_strategy": {},
        "by_ai_verdict": {
            "CONFIRM": {"win_rate": 70.0, "total": 10, "avg_pnl": 3.2},
            "REJECT": {"win_rate": 30.0, "total": 10, "avg_pnl": -2.1},
        },
        "vetoed_count": 8,
        "vetoed_win_rate": 25.0,
    })
    text = sess.calls[0]["text"]
    assert "AI verdict accuracy" in text
    assert "CONFIRM" in text and "70.0%" in text
    assert "REJECT" in text and "30.0%" in text
    # -25pp vs 50% overall → "consider veto mode"
    assert "consider veto mode" in text


# ── error handling: Telegram API failure is logged, returns False ──────────
def test_send_logs_description_on_failure(caplog):
    sess = FakeSession(status_code=400, body={"ok": False, "description": "message thread not found"})
    n = TelegramNotifier(_settings(new_signal_thread_id="999"), session=sess)
    with caplog.at_level("WARNING"):
        ok = n.send("hi", thread_id="999")
    assert ok is False
    assert "message thread not found" in caplog.text


# ── HTML escaping of dynamic content ───────────────────────────────────────
def test_reasons_are_html_escaped():
    sess = FakeSession()
    n = TelegramNotifier(_settings(), session=sess)
    n.announce_signal(_signal(reasons=["RSI < 30 & rising"]))
    assert "RSI &lt; 30 &amp; rising" in sess.calls[0]["text"]


# ── topic validation at startup ─────────────────────────────────────────────
class ThreadAwareSession:
    """Fails sendMessage for a given set of thread ids; tracks deletes."""

    def __init__(self, bad_threads=()):
        self.calls: list[dict] = []
        self.deletes: list[dict] = []
        self._bad = set(bad_threads)

    def post(self, url, json=None, timeout=None):
        if url.endswith("/deleteMessage"):
            self.deletes.append(json)
            return FakeResponse(200, {"ok": True})
        self.calls.append(json)
        tid = json.get("message_thread_id")
        if tid in self._bad:
            return FakeResponse(400, {"ok": False, "description": "message thread not found"})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 555}})


def test_validate_threads_flags_bad_and_deletes_probes():
    s = _settings(system_thread_id="1", news_thread_id="5", whale_thread_id="6")
    sess = ThreadAwareSession(bad_threads={"1"})
    n = TelegramNotifier(s, session=sess)
    result = n.validate_threads()

    bad_ids = {tid for _, tid, _ in result["bad"]}
    ok_ids = {tid for _, tid in result["ok"]}
    assert bad_ids == {"1"}
    assert ok_ids == {"5", "6"}
    # Valid probes are cleaned up; the failed one left nothing to delete.
    assert {d["message_id"] for d in sess.deletes} == {555}
    assert len(sess.deletes) == 2


def test_report_thread_validation_posts_summary_to_main_when_general_bad():
    s = _settings(system_thread_id="1", news_thread_id="5")
    sess = ThreadAwareSession(bad_threads={"1"})
    n = TelegramNotifier(s, session=sess)
    n.report_thread_validation(n.validate_threads())
    summary = sess.calls[-1]
    # General (id 1) is itself invalid -> summary must fall back to main channel.
    assert "message_thread_id" not in summary
    assert "TOPIC CHECK" in summary["text"]
    assert "System/General" in summary["text"]


def test_report_thread_validation_silent_when_all_ok(caplog):
    s = _settings(news_thread_id="5", whale_thread_id="6")
    sess = ThreadAwareSession(bad_threads=set())
    n = TelegramNotifier(s, session=sess)
    n.report_thread_validation(n.validate_threads())
    # No summary message posted (only the two probes were sent).
    assert all("TOPIC CHECK" not in (c.get("text") or "") for c in sess.calls)


# ── invalid-thread fallback to main channel ─────────────────────────────────
def test_send_falls_back_to_main_channel_on_bad_thread():
    sess = ThreadAwareSession(bad_threads={"999"})
    n = TelegramNotifier(_settings(new_signal_thread_id="999"), session=sess)
    ok = n.send("hi", thread_id="999")
    assert ok is True
    # First attempt targets the topic, retry drops message_thread_id.
    assert sess.calls[0].get("message_thread_id") == "999"
    assert "message_thread_id" not in sess.calls[1]


def test_stats_card_leads_with_r_and_window():
    sess = FakeSession()
    n = TelegramNotifier(_settings(stats_thread_id="9"), session=sess)
    n.notify_stats(
        {"window_hours": 24, "wins": 25, "losses": 38, "win_rate": 39.7,
         "avg_r": 0.02, "avg_pnl_pct": 0.07, "active": 5, "conclusive": True,
         "flat": 6, "total_traded": 63, "by_strategy": {}, "by_ai_verdict": {}},
        {"win_rate": 44.3, "avg_r": 0.05, "total_traded": 131},
    )
    text = sess.calls[0]["text"]
    assert "PERFORMANCE SUMMARY · 24h" in text
    assert "Win rate 39.7%" in text      # the window leads...
    assert "All-time 44.3%" in text      # ...cumulative is context, not headline
    assert "+0.02R" in text              # expectancy in R, comparable across coins
    assert "Flat 6" in text              # no-verdict outcomes stay visible


def test_stats_card_flags_small_samples():
    sess = FakeSession()
    n = TelegramNotifier(_settings(stats_thread_id="9"), session=sess)
    n.notify_stats({
        "wins": 0, "losses": 1, "win_rate": 0.0, "avg_r": -0.8, "avg_pnl_pct": -0.8,
        "active": 0, "conclusive": False, "total_traded": 1,
        "by_strategy": {"PREPUMP": {
            "win_rate": 0.0, "total": 1, "emitted": 1, "avg_pnl": -0.8,
            "avg_r": -0.8, "conclusive": False,
        }},
        "by_ai_verdict": {},
    })
    text = sess.calls[0]["text"]
    # A 0% win rate off one trade must not read as a verdict on the strategy.
    assert "small sample" in text and "⚠️" in text


# ── routing is reported, not left to be discovered by spam ──────────────────


def test_flow_intelligence_falls_back_to_the_news_topic():
    """The Flow digest belongs with News, not in the main channel.

    It is the noisiest recurring message the bot sends, so where it lands is
    the difference between a channel and a feed.
    """
    from wolf.config import TelegramSettings

    s = TelegramSettings(bot_token="t", chat_id="c", news_thread_id="42")
    assert s.route_flow() == "42"
    assert s.route_news() == "42"


def test_an_explicit_flow_topic_wins_over_the_news_one():
    from wolf.config import TelegramSettings

    s = TelegramSettings(bot_token="t", chat_id="c",
                         news_thread_id="42", flow_thread_id="99")
    assert s.route_flow() == "99"


def test_the_flow_topic_is_validated_like_every_other_routed_one():
    """It was posted to but never probed, so a stale id could fail in silence."""
    from wolf.config import TelegramSettings

    s = TelegramSettings(bot_token="t", chat_id="c", flow_thread_id="99")
    assert ("Flow Intelligence", "99") in s.configured_threads()


def test_what_lands_in_the_main_channel_is_named():
    """A missing topic id is otherwise invisible until the channel fills up."""
    from wolf.config import TelegramSettings

    s = TelegramSettings(bot_token="t", chat_id="c")
    unrouted = s.unrouted_destinations()
    assert "Flow Intelligence" in unrouted
    assert "News" in unrouted


def test_routing_one_topic_quiets_the_ones_that_fall_back_to_it():
    """The report follows the fallback chains rather than the raw fields.

    Setting only NEWS_THREAD_ID moves both the News digest and the Flow
    report, so neither should still be listed as landing here.
    """
    from wolf.config import TelegramSettings

    s = TelegramSettings(bot_token="t", chat_id="c", news_thread_id="42")
    unrouted = s.unrouted_destinations()
    assert "News" not in unrouted
    assert "Flow Intelligence" not in unrouted
    assert "Whale Report" in unrouted        # still has no topic of its own


def test_the_startup_card_says_what_is_landing_in_this_channel(monkeypatch):
    from wolf.config import TelegramSettings
    from wolf.notify.telegram import TelegramNotifier

    sent: list[tuple[str, str]] = []
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="c"))
    monkeypatch.setattr(n, "send", lambda text, thread="": sent.append((text, thread)))

    n.notify_startup({"sources": ["binance"], "detectors": ["SCALP"],
                      "unrouted": "News, Flow Intelligence"})
    assert "Into this channel: News, Flow Intelligence" in sent[0][0]


def test_a_fully_routed_deployment_adds_no_line(monkeypatch):
    """A line that always prints is a line nobody reads."""
    from wolf.config import TelegramSettings
    from wolf.notify.telegram import TelegramNotifier

    sent: list[tuple[str, str]] = []
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="c"))
    monkeypatch.setattr(n, "send", lambda text, thread="": sent.append((text, thread)))

    n.notify_startup({"sources": ["binance"], "detectors": ["SCALP"], "unrouted": ""})
    assert "Into this channel" not in sent[0][0]


def test_a_thread_id_of_only_whitespace_does_not_win_the_routing_chain(monkeypatch):
    """The bug: a stray space in a variable editor silently reroutes to General.

    ``_first`` picks the first truthy value, and "  " is truthy. So a
    FLOW_THREAD_ID holding nothing but a space beat a perfectly good
    NEWS_THREAD_ID, was sent to Telegram, was rejected, and every Flow digest
    fell back to the main channel — with only a log line to say why.
    """
    from wolf.config import TelegramSettings, _env_str

    monkeypatch.setenv("FLOW_THREAD_ID", "   ")
    monkeypatch.setenv("NEWS_THREAD_ID", "42")
    assert _env_str("FLOW_THREAD_ID") == ""
    assert _env_str("NEWS_THREAD_ID") == "42"

    s = TelegramSettings(bot_token="t", chat_id="c",
                         flow_thread_id=_env_str("FLOW_THREAD_ID"),
                         news_thread_id=_env_str("NEWS_THREAD_ID"))
    assert s.route_flow() == "42"


def test_a_padded_thread_id_is_trimmed_rather_than_sent_as_is(monkeypatch):
    """Telegram rejects "42\\n"; the id was right and the whitespace was not."""
    from wolf.config import _env_str

    monkeypatch.setenv("NEWS_THREAD_ID", " 42\n")
    assert _env_str("NEWS_THREAD_ID") == "42"


def test_a_blank_value_falls_back_to_the_default(monkeypatch):
    """Set-but-empty must read as unset, the way _env_int already treats it."""
    from wolf.config import _env_str

    monkeypatch.setenv("SOME_SETTING", "  ")
    assert _env_str("SOME_SETTING", "fallback") == "fallback"


def test_the_routing_chain_ignores_a_blank_id_however_it_was_built():
    """Not only from the environment: the invariant holds at the dataclass too."""
    from wolf.config import TelegramSettings

    s = TelegramSettings(bot_token="t", chat_id="c",
                         flow_thread_id="  ", news_thread_id="42")
    assert s.route_flow() == "42"
    assert "Flow Intelligence" not in s.unrouted_destinations()


def test_a_rejected_topic_is_announced_once_in_the_channel_it_lands_in(monkeypatch):
    """The retry must not make the misrouting invisible.

    Falling back keeps the message from being lost, which is right. Saying
    nothing turns a rejected topic into a permanent, unexplained reroute — a
    recurring digest quietly becomes the main channel's feed and every reading
    of "why is this here" has to be a guess.
    """
    from wolf.config import TelegramSettings
    from wolf.notify.telegram import TelegramNotifier

    posts: list[tuple[str, str]] = []

    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="c",
                                          news_thread_id="42"))

    def fake_post(text, thread_id=""):
        posts.append((text, thread_id))
        if thread_id:
            return False, "Bad Request: message thread not found", None
        return True, "", 1

    monkeypatch.setattr(n, "_post", fake_post)

    assert n.send("first digest", "42") is True
    notice = [t for t, thread in posts if "Topic unavailable" in t]
    assert len(notice) == 1
    assert "42" in notice[0]
    assert "message thread not found" in notice[0]

    # A second message through the same broken topic must not repeat the notice.
    n.send("second digest", "42")
    assert len([t for t, _ in posts if "Topic unavailable" in t]) == 1


def test_a_working_topic_announces_nothing(monkeypatch):
    from wolf.config import TelegramSettings
    from wolf.notify.telegram import TelegramNotifier

    posts: list[tuple[str, str]] = []
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="c"))
    monkeypatch.setattr(n, "_post",
                        lambda text, thread_id="": (posts.append((text, thread_id)), (True, "", 1))[1])

    n.send("digest", "42")
    assert not any("Topic unavailable" in t for t, _ in posts)
