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
