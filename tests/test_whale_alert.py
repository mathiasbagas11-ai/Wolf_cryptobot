"""Tests for the whale-coordination alert → 👁 Whale Report topic.

Event-driven, and separate from the Flow Intelligence digest on purpose: this
fires when wallets pile in, the digest reports positioning on a timer.
"""

from __future__ import annotations

import re

from wolf.reports.whale_alert import (
    build_coordination_alerts,
    format_coordination_alert,
    short_address,
)


def _plain(html: str) -> str:
    return re.sub(r"</?(b|i|code)>", "", html)


def _doc(**coins) -> dict:
    return {"ts": "2026-08-20T04:00:00+00:00", "coins": coins}


_EVENT = {
    "direction": "LONG",
    "wallet_count": 4,
    "notional_usd": 2_400_000,
    "wallets": [
        {"addr": "0x1234567890abcdef", "notional": 900_000, "is_new": True},
        {"addr": "0xfedcba0987654321", "notional": 800_000, "is_new": False},
    ],
}


# ── formatting ────────────────────────────────────────────────────────────
def test_alert_states_coin_direction_count_and_size():
    text = _plain(format_coordination_alert("SOL", "LONG", 4, 2_400_000))

    assert "WHALE COORDINATION" in text
    assert "$SOL" in text and "LONG" in text
    assert "4 wallet" in text
    assert "$2.40M" in text


def test_long_and_short_get_distinct_labels():
    assert "AKUMULASI" in format_coordination_alert("SOL", "LONG", 3, 1e6)
    assert "DISTRIBUSI" in format_coordination_alert("SOL", "SHORT", 3, 1e6)


def test_alert_lists_wallets_and_marks_new_versus_added():
    text = _plain(format_coordination_alert("SOL", "LONG", 4, 2_400_000, _EVENT["wallets"]))

    assert "0x1234…cdef" in text
    assert "🆕" in text and "➕" in text


def test_wallet_list_is_truncated():
    wallets = [{"addr": f"0x{i:040d}", "notional": 1e5, "is_new": True} for i in range(12)]
    text = format_coordination_alert("SOL", "LONG", 12, 1.2e6, wallets)

    assert text.count("<code>") == 5


def test_alert_survives_missing_wallet_detail():
    text = format_coordination_alert("SOL", "LONG", 3, 1e6, None)
    assert "$SOL" in text
    text = format_coordination_alert("SOL", "LONG", 3, 1e6, ["not-a-dict"])
    assert "$SOL" in text


def test_alert_escapes_untrusted_coin_names():
    assert "<script>" not in format_coordination_alert("<script>x</script>", "LONG", 3, 1e6)


def test_alert_carries_no_trade_instruction():
    """The whale room reports positioning; what to do about it is the detectors' call."""
    text = _plain(format_coordination_alert("SOL", "LONG", 4, 2_400_000, _EVENT["wallets"])).lower()
    for banned in ("entry", "target", "stop loss", " sl ", " tp ", "buy", "sell"):
        assert banned not in text


def test_short_address_leaves_short_strings_alone():
    assert short_address("0x1234567890abcdef") == "0x1234…cdef"
    assert short_address("0xabc") == "0xabc"
    assert short_address("") == ""
    assert short_address(None) == ""


# ── batch building ────────────────────────────────────────────────────────
def test_no_events_produces_no_alerts():
    """The normal case — most scans see no coordination at all."""
    assert build_coordination_alerts(_doc()) == []
    assert build_coordination_alerts({}) == []
    assert build_coordination_alerts({"coins": "junk"}) == []


def test_one_alert_per_coordinated_coin():
    alerts = build_coordination_alerts(_doc(SOL=_EVENT, HYPE=dict(_EVENT, direction="SHORT")))
    assert len(alerts) == 2


def test_alerts_are_ordered_by_notional():
    small = dict(_EVENT, notional_usd=100_000)
    big = dict(_EVENT, notional_usd=9_000_000)
    alerts = build_coordination_alerts(_doc(SMALL=small, BIG=big))

    assert "$BIG" in alerts[0] and "$SMALL" in alerts[1]


def test_malformed_rows_are_skipped_not_fatal():
    alerts = build_coordination_alerts(_doc(SOL=_EVENT, BAD="not-a-dict"))
    assert len(alerts) == 1
