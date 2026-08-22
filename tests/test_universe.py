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


def _row(symbol, vol):
    return {"symbol": symbol, "quote_volume": vol, "change_pct": 1.0, "price": 1.0}


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
