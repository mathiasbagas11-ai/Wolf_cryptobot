"""Tests for re-grading a resolved sample under a different stop rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.config import LadderSettings, TrackerSettings
from wolf.models import Candle, Status
from wolf.tracker import OUTCOMES_KEY, Tracker
from wolf.whatif import compare_stop_rules, render

LADDER_1_3 = [
    {"level": 1, "price": 105, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 110, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 115, "allocation": 0.2, "r_multiple": 3.0},
]


def _run(store, fake_client, path, ladder_cfg):
    """Grade one LONG (entry 100 / stop 95) over ``path`` under ``ladder_cfg``."""
    tracker = Tracker(store, fake_client, TrackerSettings(), ladder=ladder_cfg)
    sig = tracker.record_signal(
        "BTCUSDT", "MOMENTUM", "LONG", 100, tp=115, sl=95,
        entry_mode="MOMENTUM_NOW", timeframe="15m", tps=LADDER_1_3,
        entry_quoted_live=True,
    )
    now_ms = int(datetime.fromisoformat(sig.created_at).timestamp() * 1000)
    fake_client.klines["BTCUSDT"] = [
        # A bar from before the signal, so the fetched history demonstrably
        # reaches back to the entry — which is what the replay checks for. It
        # sits below every rung and above the stop, so it changes no outcome.
        Candle(time=now_ms - 900_000, open=100, high=100, low=100, close=100, volume=100.0)
    ] + [
        Candle(time=now_ms + (i + 1) * 900_000, open=o, high=h, low=l, close=c, volume=100.0)
        for i, (o, h, l, c) in enumerate(path)
    ]
    return tracker.check_pending()[0]


# The case the two rules are supposed to disagree about: TP2 fills, then price
# slides all the way back to entry without ever reaching TP3.
_TP2_THEN_FADE = [
    (100, 106, 100, 105),   # TP1
    (105, 111, 104, 110),   # TP2
    (110, 111,  99, 100),   # fades to entry
]


def test_a_frozen_breakeven_stop_hands_the_last_slice_back(store, fake_client):
    """Under "breakeven" the stop never moves again after TP1.

    So a trade that got as far as TP2 still returns its remaining 20% to entry,
    booking only what the first two rungs banked.
    """
    r = _run(store, fake_client, _TP2_THEN_FADE, LadderSettings(stop_advance="breakeven"))
    assert r.tps_hit == [1, 2]
    assert r.r_multiple == 1.1     # .5x1R + .3x2R + .2x0R


def test_the_ladder_rule_protects_the_rung_below(store, fake_client):
    """Under "ladder" TP2 pushes the stop up to TP1, so the last slice pays 1R."""
    r = _run(store, fake_client, _TP2_THEN_FADE, LadderSettings(stop_advance="ladder"))
    assert r.tps_hit == [1, 2]
    assert r.r_multiple == 1.3     # .5x1R + .3x2R + .2x1R


def test_the_ladder_rule_gives_up_a_run_that_dips_first(store, fake_client):
    """The cost side, which is why this is a setting and not a rewrite.

    Price reaches TP2, slips under TP1, then runs to TP3. The advanced stop
    takes it out at TP1 for 1.3R; the frozen one rides it to a full 1.7R.
    """
    dip_then_run = [
        (100, 106, 100, 105),
        (105, 111, 104, 110),   # TP2
        (110, 110, 104, 106),   # dips under TP1 (105)
        (106, 116, 105, 115),   # then runs to TP3
    ]
    frozen = _run(store, fake_client, dip_then_run, LadderSettings(stop_advance="breakeven"))
    assert frozen.tps_hit == [1, 2, 3] and frozen.r_multiple == 1.7

    fake_client.klines.clear()
    advanced = _run(store, fake_client, dip_then_run, LadderSettings(stop_advance="ladder"))
    assert advanced.tps_hit == [1, 2] and advanced.r_multiple == 1.3


def test_tp1_still_means_breakeven_under_both_rules(store, fake_client):
    """The first rung behaves identically — "ladder" only changes later rungs."""
    tp1_then_fade = [(100, 106, 100, 105), (105, 106, 99, 100)]
    for rule, expected in (("breakeven", 0.5), ("ladder", 0.5)):
        fake_client.klines.clear()
        r = _run(store, fake_client, tp1_then_fade, LadderSettings(stop_advance=rule))
        assert r.tps_hit == [1] and r.r_multiple == expected, rule


def test_the_comparison_scores_both_rules_on_the_same_trades(store, fake_client):
    """The whole point is that only the rule differs between the two columns."""
    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    tracker = Tracker(store, fake_client, TrackerSettings())
    report = compare_stop_rules(tracker)
    assert report["error"] == ""
    assert report["sample"] == 1
    assert report["scored"] == 1 and report["skipped"] == 0
    scores = {r.rule: r.mean_r for r in report["results"]}
    assert scores == {"breakeven": 1.1, "ladder": 1.3}
    assert "ladder leads by +0.200R/trade" in render(report)


def test_an_empty_history_says_so_rather_than_reporting_zero(store, fake_client):
    tracker = Tracker(store, fake_client, TrackerSettings())
    report = compare_stop_rules(tracker)
    assert report["results"] == []
    assert "no graded signals" in report["error"]
    assert "no graded signals" in render(report)


def test_missing_price_history_refuses_to_compare(store, fake_client):
    """No answer beats an answer computed from whatever candles came back."""
    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    fake_client.klines.clear()          # candles no longer fetchable
    report = compare_stop_rules(Tracker(store, fake_client, TrackerSettings()))
    assert report["results"] == []
    assert "reaching back to its entry" in report["error"]


def test_history_that_starts_after_the_entry_is_refused(store, fake_client):
    """The klines API answers with the newest N bars, not a date range.

    Asking for as many bars as a trade lasted returns *today's* last N bars, and
    for an older signal every one of them sits after its start — so the filter
    passes them all and the replay grades the setup against unrelated prices.
    A history whose first bar begins after the entry is not this trade's
    history, however plausible the numbers it would produce.
    """
    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    first = fake_client.klines["BTCUSDT"][0]
    fake_client.klines["BTCUSDT"] = [
        c for c in fake_client.klines["BTCUSDT"] if c.time > first.time
    ]
    report = compare_stop_rules(Tracker(store, fake_client, TrackerSettings()))
    assert report["results"] == []
    assert "reaching back to its entry" in report["error"]


def test_the_endpoint_renders_the_comparison(store, fake_client):
    from fastapi.testclient import TestClient
    from wolf.api.app import create_app

    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    tracker = Tracker(store, fake_client, TrackerSettings())

    class _App:
        pass
    app_obj = _App()
    app_obj.tracker = tracker
    app_obj.store = store
    from wolf.config import Settings
    app_obj.settings = Settings()
    app_obj.notifier = type("N", (), {"enabled": False})()
    app_obj.screener = type("S", (), {"_validator": None})()

    client = TestClient(create_app(app_obj))
    body = client.get("/whatif/stops").text
    assert "breakeven" in body and "ladder" in body
    assert "ladder leads by +0.200R/trade" in body


# ── Telegram commands ───────────────────────────────────────────────────────
def _router_app(store, fake_client):
    from types import SimpleNamespace
    from wolf.config import Settings

    return SimpleNamespace(
        analyze=None,
        tracker=Tracker(store, fake_client, TrackerSettings()),
        settings=Settings(),
        screener=SimpleNamespace(_validator=None),
        account=None,
        learning=None,
    )


def test_whatif_is_reachable_as_a_telegram_command(store, fake_client):
    """The API is not exposed on this deployment; Telegram is the way in."""
    from wolf.notify.commands import CommandRouter

    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    reply = CommandRouter(_router_app(store, fake_client)).handle("/whatif")
    assert "breakeven" in reply and "ladder" in reply
    assert "ladder leads by +0.200R/trade" in reply


def test_diag_is_reachable_as_a_telegram_command(store, fake_client):
    """The digest already arrives daily; this asks for it between reports."""
    from wolf.notify.commands import CommandRouter

    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    router = CommandRouter(_router_app(store, fake_client))
    assert "WOLF-DIAG" in router.handle("/diag")
    assert "WOLF-DIAG" in router.handle("/diag 48")
    assert "Usage" in router.handle("/diag banyak")


def test_both_commands_are_listed_in_help(store, fake_client):
    from wolf.notify.commands import CommandRouter

    help_text = CommandRouter(_router_app(store, fake_client)).handle("/help")
    assert "/whatif" in help_text and "/diag" in help_text


def test_both_rules_always_score_the_identical_set_of_trades(store, fake_client):
    """A signal counts under every rule or under none — never just one.

    The advanced stop terminates trades sooner, so it resolves some that the
    frozen stop leaves running. Scoring each rule over whatever it managed to
    grade let the columns hold different trades: three trades' difference in
    membership was worth 4.16R, which was the entire reported gap between them.
    """
    from wolf import whatif

    stopped_out = [(100, 101, 94, 96)]        # straight to the stop, resolves
    for path in (_TP2_THEN_FADE, stopped_out):
        fake_client.klines.clear()
        _run(store, fake_client, path, LadderSettings())

    tracker = Tracker(store, fake_client, TrackerSettings())
    real = whatif._regrade_one
    calls = {"n": 0}

    def flaky(probe, sig, candles):
        # Fail for exactly one rule on one signal — the asymmetry that bit.
        calls["n"] += 1
        if calls["n"] == 2:
            return None
        return real(probe, sig, candles)

    whatif._regrade_one = flaky
    try:
        report = whatif.compare_stop_rules(tracker)
    finally:
        whatif._regrade_one = real

    counts = {r.rule: r.n for r in report["results"]}
    assert len(set(counts.values())) == 1, counts


def test_the_render_says_how_much_of_the_sample_it_could_use(store, fake_client):
    """Coverage is part of the answer: 189 of 198 is a different claim to 198."""
    _run(store, fake_client, _TP2_THEN_FADE, LadderSettings())
    text = render(compare_stop_rules(Tracker(store, fake_client, TrackerSettings())))
    assert "scored 1 of 1" in text
    assert "0 without usable history" in text
