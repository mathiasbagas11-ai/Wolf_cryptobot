"""Tests for the Hyperliquid whale-coordination collector.

The detection rule is the interesting part and it is pure, so the threshold and
cooldown edges are tested directly on dicts. The collector is driven through a
stub session to prove the scan is *global* — one leaderboard read per scan, not
one per symbol.
"""

from __future__ import annotations

from wolf.onchain.whale_hyperliquid import (
    STATE_KEY,
    WhaleHyperliquidCollector,
    detect_whale_coordination,
    parse_leaderboard,
    parse_positions,
    summarise_coin_bias,
)


def _pos(side: str = "LONG", notional: float = 100_000.0, entry: float = 100.0) -> dict:
    return {"side": side, "size": notional / entry, "entry_px": entry, "notional": notional}


def _wallets(n: int, coin: str = "SOL", side: str = "LONG") -> dict[str, dict]:
    return {f"0xwallet{i}": {coin: _pos(side)} for i in range(n)}


# ── detect_whale_coordination: the ≥3-wallet rule ─────────────────────────
def test_three_wallets_same_direction_is_coordination():
    events = detect_whale_coordination({}, _wallets(3))
    assert len(events) == 1
    assert events[0]["coin"] == "SOL"
    assert events[0]["direction"] == "LONG"
    assert events[0]["wallet_count"] == 3
    assert events[0]["notional_usd"] == 300_000.0


def test_two_wallets_is_not_coordination():
    """Below the threshold is *no* signal, not a weak one."""
    assert detect_whale_coordination({}, _wallets(2)) == []


def test_threshold_is_configurable():
    assert detect_whale_coordination({}, _wallets(2), min_wallets=2) != []
    assert detect_whale_coordination({}, _wallets(4), min_wallets=5) == []


def test_wallets_must_agree_on_direction():
    """Three wallets on one coin, split 2 long / 1 short, is not coordination."""
    positions = {
        "0xa": {"SOL": _pos("LONG")},
        "0xb": {"SOL": _pos("LONG")},
        "0xc": {"SOL": _pos("SHORT")},
    }
    assert detect_whale_coordination({}, positions) == []


def test_opposite_sides_reported_separately_when_both_qualify():
    positions = {f"0xl{i}": {"SOL": _pos("LONG")} for i in range(3)}
    positions.update({f"0xs{i}": {"SOL": _pos("SHORT", 50_000.0)} for i in range(3)})
    events = detect_whale_coordination({}, positions)
    assert {e["direction"] for e in events} == {"LONG", "SHORT"}
    # Sorted by notional: the larger side leads.
    assert events[0]["direction"] == "LONG"


def test_unchanged_positions_are_not_events():
    """Holding a position is not the same as opening one."""
    held = _wallets(3)
    assert detect_whale_coordination(held, held) == []


def test_material_adds_count_but_noise_does_not():
    before = _wallets(3)
    grown = {addr: {"SOL": _pos("LONG", 130_000.0)} for addr in before}
    assert detect_whale_coordination(before, grown)[0]["wallet_count"] == 3

    wiggle = {addr: {"SOL": _pos("LONG", 102_000.0)} for addr in before}
    assert detect_whale_coordination(before, wiggle) == []


def test_reduced_positions_are_not_events():
    before = _wallets(3)
    trimmed = {addr: {"SOL": _pos("LONG", 40_000.0)} for addr in before}
    assert detect_whale_coordination(before, trimmed) == []


def test_new_wallet_entering_an_existing_coin_counts():
    before = {"0xa": {"SOL": _pos()}}
    after = {"0xa": {"SOL": _pos()}, "0xb": {"SOL": _pos()},
             "0xc": {"SOL": _pos()}, "0xd": {"SOL": _pos()}}
    event = detect_whale_coordination(before, after)[0]
    assert event["wallet_count"] == 3, "the unchanged wallet must not be counted"


def test_events_sorted_by_notional_across_coins():
    positions = {f"0xa{i}": {"SOL": _pos("LONG", 10_000.0)} for i in range(3)}
    positions.update({f"0xb{i}": {"ETH": _pos("LONG", 900_000.0)} for i in range(3)})
    events = detect_whale_coordination({}, positions)
    assert [e["coin"] for e in events] == ["ETH", "SOL"]


def test_empty_inputs_are_safe():
    assert detect_whale_coordination({}, {}) == []


# ── cooldown ──────────────────────────────────────────────────────────────
def test_cooldown_suppresses_repeat_alert_for_the_same_coin(store):
    collector = WhaleHyperliquidCollector(store, cooldown_min=60.0)
    events = detect_whale_coordination({}, _wallets(3))

    assert collector._apply_cooldown(events, now=1_000.0), "first alert passes"
    assert collector._apply_cooldown(events, now=1_000.0 + 30 * 60) == [], "still cooling"


def test_cooldown_expires_and_allows_a_new_alert(store):
    collector = WhaleHyperliquidCollector(store, cooldown_min=60.0)
    events = detect_whale_coordination({}, _wallets(3))

    collector._apply_cooldown(events, now=1_000.0)
    assert collector._apply_cooldown(events, now=1_000.0 + 61 * 60), "cooldown elapsed"


def test_cooldown_is_per_coin(store):
    collector = WhaleHyperliquidCollector(store, cooldown_min=60.0)
    sol = detect_whale_coordination({}, _wallets(3, "SOL"))
    eth = detect_whale_coordination({}, _wallets(3, "ETH"))

    collector._apply_cooldown(sol, now=1_000.0)
    passed = collector._apply_cooldown(eth, now=1_000.0)

    assert [e["coin"] for e in passed] == ["ETH"]


def test_cooldown_state_is_per_instance(store):
    """The original kept cooldowns in a module-level dict shared by everything."""
    events = detect_whale_coordination({}, _wallets(3))
    WhaleHyperliquidCollector(store)._apply_cooldown(events, now=1_000.0)
    assert WhaleHyperliquidCollector(store)._apply_cooldown(events, now=1_000.0)


# ── payload parsing ───────────────────────────────────────────────────────
def test_parse_leaderboard_ranks_by_all_time_pnl():
    payload = {"leaderboardRows": [
        {"ethAddress": "0xsmall", "pnl": {"allTime": 10}},
        {"ethAddress": "0xbig", "pnl": {"allTime": 9_000}},
        {"ethAddress": "0xmid", "pnl": {"allTime": 500}},
    ]}
    assert parse_leaderboard(payload) == ["0xbig", "0xmid", "0xsmall"]


def test_parse_leaderboard_respects_limit_and_skips_addressless_rows():
    payload = {"leaderboardRows": [
        {"ethAddress": "0xa", "pnl": {"allTime": 3}},
        {"pnl": {"allTime": 99}},
        {"address": "0xb", "pnl": {"allTime": 2}},
    ]}
    assert parse_leaderboard(payload, limit=1) == ["0xa"]
    assert parse_leaderboard(payload) == ["0xa", "0xb"]


def test_parse_leaderboard_rejects_junk():
    assert parse_leaderboard(None) == []
    assert parse_leaderboard({"leaderboardRows": "nope"}) == []


def test_parse_positions_reads_side_and_notional():
    payload = {"assetPositions": [
        {"position": {"coin": "SOL", "szi": "1000", "entryPx": "150"}},
        {"position": {"coin": "ETH", "szi": "-20", "entryPx": "3000"}},
    ]}
    parsed = parse_positions(payload)
    assert parsed["SOL"]["side"] == "LONG"
    assert parsed["SOL"]["notional"] == 150_000.0
    assert parsed["ETH"]["side"] == "SHORT"
    assert parsed["ETH"]["notional"] == 60_000.0


def test_parse_positions_drops_dust_and_closed_positions():
    payload = {"assetPositions": [
        {"position": {"coin": "TINY", "szi": "1", "entryPx": "10"}},      # $10
        {"position": {"coin": "FLAT", "szi": "0", "entryPx": "1000"}},    # closed
        {"position": {"coin": "REAL", "szi": "10", "entryPx": "10000"}},  # $100k
    ]}
    assert set(parse_positions(payload)) == {"REAL"}


def test_parse_positions_rejects_junk():
    assert parse_positions(None) == {}
    assert parse_positions({"assetPositions": [{"nope": 1}]}) == {}


# ── summarise_coin_bias ───────────────────────────────────────────────────
def test_summarise_coin_bias_splits_long_and_short():
    positions = {
        "0xa": {"SOL": _pos("LONG", 100_000.0)},
        "0xb": {"SOL": _pos("LONG", 200_000.0)},
        "0xc": {"SOL": _pos("SHORT", 50_000.0)},
    }
    bias = summarise_coin_bias(positions)["SOL"]
    assert bias["long_count"] == 2 and bias["short_count"] == 1
    assert bias["long_notional"] == 300_000.0
    assert bias["short_notional"] == 50_000.0


# ── collector end-to-end (stubbed HTTP) ───────────────────────────────────
class _Resp:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _StubSession:
    """Serves a fixed leaderboard and a per-scan positions map."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.positions: dict[str, dict] = {}
        self.leaderboard_calls = 0
        self.wallet_calls = 0

    def get(self, url, timeout=None, headers=None):
        self.leaderboard_calls += 1
        return _Resp({"leaderboardRows": [
            {"ethAddress": a, "pnl": {"allTime": 1000 - i}}
            for i, a in enumerate(self.addresses)
        ]})

    def post(self, url, json=None, timeout=None, headers=None):
        self.wallet_calls += 1
        user = (json or {}).get("user", "")
        return _Resp({"assetPositions": self.positions.get(user, [])})


def _hl_position(coin: str, szi: float, px: float = 1_000.0) -> dict:
    return {"position": {"coin": coin, "szi": str(szi), "entryPx": str(px)}}


def test_first_scan_captures_baseline_without_alerting(store):
    session = _StubSession(["0xa", "0xb", "0xc"])
    session.positions = {a: [_hl_position("SOL", 100)] for a in session.addresses}
    collector = WhaleHyperliquidCollector(store, session=session, request_pause=0)

    doc = collector.scan()

    assert doc["coins"] == {}, "every position looks new on the first scan"
    assert doc["bias"]["SOL"]["long_count"] == 3
    assert doc["wallets_scanned"] == 3


def test_second_scan_detects_coordination_and_persists_it(store):
    session = _StubSession(["0xa", "0xb", "0xc"])
    collector = WhaleHyperliquidCollector(store, session=session, request_pause=0)

    collector.scan()                                    # baseline: nobody in SOL
    session.positions = {a: [_hl_position("SOL", 100, 24_000.0)] for a in session.addresses}
    doc = collector.scan()

    assert doc["coins"]["SOL"] == {
        "direction": "LONG", "wallet_count": 3, "notional_usd": 7_200_000,
    }
    assert store.read(STATE_KEY)["coins"]["SOL"]["direction"] == "LONG"


def test_scan_reads_the_leaderboard_once_not_once_per_symbol(store):
    """The whole point of the collector shape: one global scan per run."""
    session = _StubSession(["0xa", "0xb", "0xc"])
    session.positions = {a: [_hl_position("SOL", 100), _hl_position("ETH", 50)]
                         for a in session.addresses}
    collector = WhaleHyperliquidCollector(store, session=session, request_pause=0)

    collector.scan()

    assert session.leaderboard_calls == 1
    assert session.wallet_calls == 3, "one request per wallet, covering every coin"


def test_scan_covers_every_coin_in_one_pass(store):
    session = _StubSession(["0xa", "0xb", "0xc"])
    collector = WhaleHyperliquidCollector(store, session=session, request_pause=0)
    collector.scan()
    session.positions = {a: [_hl_position("SOL", 100), _hl_position("ETH", 50)]
                         for a in session.addresses}

    doc = collector.scan()

    assert set(doc["coins"]) == {"SOL", "ETH"}


def test_repeat_scan_does_not_realert_within_cooldown(store):
    session = _StubSession(["0xa", "0xb", "0xc"])
    collector = WhaleHyperliquidCollector(store, session=session, request_pause=0)
    collector.scan()
    session.positions = {a: [_hl_position("SOL", 100)] for a in session.addresses}
    assert collector.scan()["coins"], "first detection alerts"

    # Everyone adds again — still the same build-up, still inside the cooldown.
    session.positions = {a: [_hl_position("SOL", 200)] for a in session.addresses}
    assert collector.scan()["coins"] == {}


def test_empty_leaderboard_keeps_the_previous_snapshot(store):
    session = _StubSession(["0xa", "0xb", "0xc"])
    collector = WhaleHyperliquidCollector(store, session=session, request_pause=0)
    collector.scan()
    session.positions = {a: [_hl_position("SOL", 100)] for a in session.addresses}
    collector.scan()
    before = store.read(STATE_KEY)

    session.addresses = []
    assert collector.scan() == before
    assert store.read(STATE_KEY) == before


def test_collectors_do_not_share_baseline_state(store):
    """Module-level state made the old tracker a singleton; this must not."""
    session = _StubSession(["0xa", "0xb", "0xc"])
    session.positions = {a: [_hl_position("SOL", 100)] for a in session.addresses}
    WhaleHyperliquidCollector(store, session=session, request_pause=0).scan()

    fresh = WhaleHyperliquidCollector(store, session=session, request_pause=0)
    assert fresh.scan()["coins"] == {}, "a new collector starts from its own baseline"
