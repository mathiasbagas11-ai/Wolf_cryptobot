"""Tests for dynamic universe selection."""

from __future__ import annotations

from wolf.universe import CORE_MAJORS, UniverseProvider


class _OverviewClient:
    def __init__(self, rows, raise_exc=False):
        self._rows = rows
        self._raise = raise_exc

    def get_market_overview(self):
        if self._raise:
            raise RuntimeError("boom")
        return self._rows


def _row(symbol, vol, change=1.0):
    return {"symbol": symbol, "quote_volume": vol, "change_pct": change, "price": 1.0}


# ── Mover lane ───────────────────────────────────────────────────────────
#
# Ranking by 24h volume is a lagging filter, and it lags where it costs most: a
# coin enters the top-N *because* it already pumped, so a token can run +127% in
# a day without the bot having evaluated one of its candles. These cover the
# second lane, which trades a lower liquidity floor for seeing the move earlier.

def test_mover_lane_scans_a_symbol_below_the_volume_cut():
    rows = [_row(f"MAJOR{i}USDT", 500_000_000) for i in range(30)]
    rows.append(_row("SOPHUSDT", 8_000_000, change=127.0))
    prov = UniverseProvider(
        _OverviewClient(rows), top_n=30, min_quote_volume=10_000_000,
        mover_min_quote_volume=3_000_000, mover_min_change_pct=8.0,
    )
    syms = prov.symbols()
    assert "SOPHUSDT" in syms, "a +127% mover was invisible to every detector"


def test_mover_lane_respects_its_own_liquidity_floor():
    rows = [_row("DUSTUSDT", 500_000, change=90.0)]
    syms = UniverseProvider(
        _OverviewClient(rows), mover_min_quote_volume=3_000_000
    ).symbols()
    assert "DUSTUSDT" not in syms


def test_mover_lane_ignores_a_quiet_symbol():
    rows = [_row("SLEEPYUSDT", 8_000_000, change=1.5)]
    syms = UniverseProvider(
        _OverviewClient(rows), mover_min_quote_volume=3_000_000, mover_min_change_pct=8.0
    ).symbols()
    assert "SLEEPYUSDT" not in syms


def test_mover_lane_takes_both_directions():
    """A token down 20% is a candidate for the short detectors exactly the way
    one up 20% is for the long side."""
    rows = [_row("DUMPUSDT", 8_000_000, change=-25.0)]
    syms = UniverseProvider(
        _OverviewClient(rows), mover_min_quote_volume=3_000_000
    ).symbols()
    assert "DUMPUSDT" in syms


def test_mover_lane_never_duplicates_a_symbol():
    rows = [_row("PEPEUSDT", 90_000_000, change=40.0)]
    syms = UniverseProvider(
        _OverviewClient(rows), top_n=10, min_quote_volume=10_000_000,
        mover_min_quote_volume=3_000_000,
    ).symbols()
    assert syms.count("PEPEUSDT") == 1
    assert len(syms) == len(set(syms))


def test_mover_lane_is_capped():
    rows = [_row(f"ALT{i}USDT", 5_000_000, change=50.0 + i) for i in range(40)]
    syms = UniverseProvider(
        _OverviewClient(rows), top_n=0, min_quote_volume=10_000_000,
        mover_top_n=5, mover_min_quote_volume=3_000_000,
    ).symbols()
    assert len(syms) == len(CORE_MAJORS) + 5


def test_mover_lane_can_be_disabled():
    rows = [_row("SOPHUSDT", 8_000_000, change=127.0)]
    syms = UniverseProvider(
        _OverviewClient(rows), mover_lane=False, mover_min_quote_volume=3_000_000
    ).symbols()
    assert "SOPHUSDT" not in syms


def test_ranks_by_volume_and_includes_core():
    rows = [
        _row("PEPEUSDT", 90_000_000),
        _row("WIFUSDT", 80_000_000),
        _row("FLOKIUSDT", 70_000_000),
        _row("BTCUSDT", 1_000_000_000),
    ]
    syms = UniverseProvider(_OverviewClient(rows), top_n=2, min_quote_volume=10_000_000).symbols()
    # Core majors always present, even those absent from the snapshot.
    for major in CORE_MAJORS:
        assert major in syms
    # Top-2 volume leaders (excluding core BTC already counted) rotate in.
    assert "PEPEUSDT" in syms and "WIFUSDT" in syms


def test_filters_below_min_volume():
    rows = [_row("PEPEUSDT", 5_000_000), _row("WIFUSDT", 50_000_000)]
    syms = UniverseProvider(_OverviewClient(rows), top_n=10, min_quote_volume=10_000_000).symbols()
    assert "WIFUSDT" in syms
    assert "PEPEUSDT" not in syms


def test_excludes_stablecoin_and_nonquote_pairs():
    rows = [
        _row("USDCUSDT", 500_000_000),   # stable base — excluded
        _row("FDUSDUSDT", 400_000_000),  # stable base — excluded
        _row("ETHBTC", 300_000_000),     # wrong quote — excluded
        _row("WIFUSDT", 50_000_000),
    ]
    syms = UniverseProvider(_OverviewClient(rows), top_n=10, min_quote_volume=10_000_000).symbols()
    assert "USDCUSDT" not in syms
    assert "FDUSDUSDT" not in syms
    assert "ETHBTC" not in syms
    assert "WIFUSDT" in syms


def test_falls_back_to_core_on_empty_or_error():
    assert UniverseProvider(_OverviewClient([])).symbols() == list(CORE_MAJORS)
    assert UniverseProvider(_OverviewClient([], raise_exc=True)).symbols() == list(CORE_MAJORS)


def test_no_duplicate_when_leader_is_core():
    rows = [_row("BTCUSDT", 1_000_000_000), _row("WIFUSDT", 50_000_000)]
    syms = UniverseProvider(_OverviewClient(rows), top_n=10, min_quote_volume=10_000_000).symbols()
    assert syms.count("BTCUSDT") == 1
