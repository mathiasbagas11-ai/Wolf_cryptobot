"""Phase 1 verification — universe filter logic (network-free, canned payloads)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from anomaly.universe import (
    DCA_HOLDINGS,
    MCAP_MAX,
    MCAP_MIN,
    VOLUME_MIN,
    build_universe,
    filter_universe,
)

NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)
OLD_ATL = (NOW - timedelta(days=400)).isoformat()   # well-aged listing
NEW_ATL = (NOW - timedelta(days=5)).isoformat()     # freshly listed


def _row(**over):
    base = {
        "id": "coin", "symbol": "coin", "name": "Coin",
        "current_price": 1.0, "market_cap": 100_000_000,
        "total_volume": 10_000_000, "fully_diluted_valuation": 150_000_000,
        "price_change_percentage_24h": 1.5, "atl_date": OLD_ATL,
    }
    base.update(over)
    return base


def test_good_coin_passes_with_all_fields():
    out = filter_universe([_row(id="aaa", symbol="aaa")], now=NOW)
    assert len(out) == 1
    c = out[0]
    assert set(c) == {
        "id", "symbol", "name", "current_price", "market_cap",
        "total_volume", "fdv", "price_change_percentage_24h", "in_dca_sleeve",
    }
    assert c["symbol"] == "AAA"           # uppercased
    assert c["fdv"] == 150_000_000
    assert c["in_dca_sleeve"] is False


def test_stablecoins_dropped():
    rows = [_row(id="t", symbol="usdt"), _row(id="c", symbol="usdc"),
            _row(id="d", symbol="dai")]
    assert filter_universe(rows, now=NOW) == []


def test_mcap_band_enforced():
    rows = [
        _row(id="lo", symbol="lo", market_cap=MCAP_MIN - 1),   # too small
        _row(id="hi", symbol="hi", market_cap=MCAP_MAX + 1),   # too big
        _row(id="ok", symbol="ok", market_cap=MCAP_MIN + 1),   # in band
    ]
    out = {c["symbol"] for c in filter_universe(rows, now=NOW)}
    assert out == {"OK"}


def test_thin_volume_dropped():
    rows = [_row(id="thin", symbol="thin", total_volume=VOLUME_MIN - 1)]
    assert filter_universe(rows, now=NOW) == []


def test_fresh_listing_dropped_but_null_atl_kept():
    rows = [
        _row(id="new", symbol="new", atl_date=NEW_ATL),   # < 30d → drop
        _row(id="null", symbol="null", atl_date=None),    # unknown age → keep
    ]
    out = {c["symbol"] for c in filter_universe(rows, now=NOW)}
    assert out == {"NULL"}


def test_dca_holdings_flagged_not_dropped_even_off_band():
    # BTC is above MCAP_MAX and would normally be dropped — must survive & flag.
    rows = [_row(id="bitcoin", symbol="btc", market_cap=1_500_000_000_000,
                 total_volume=50_000_000_000)]
    out = filter_universe(rows, now=NOW)
    assert len(out) == 1
    assert out[0]["symbol"] == "BTC"
    assert out[0]["in_dca_sleeve"] is True


def test_all_dca_symbols_recognised():
    rows = [_row(id=s.lower(), symbol=s.lower(), market_cap=5_000_000_000)
            for s in DCA_HOLDINGS]
    out = filter_universe(rows, now=NOW)
    assert len(out) == len(DCA_HOLDINGS)
    assert all(c["in_dca_sleeve"] for c in out)


def test_duplicate_ids_deduped():
    rows = [_row(id="dup", symbol="dup"), _row(id="dup", symbol="dup")]
    assert len(filter_universe(rows, now=NOW)) == 1


def test_build_universe_uses_cache(tmp_path):
    cache = tmp_path / "universe.json"
    cache.write_text(
        '{"cached_at": "%s", "coins": [{"id": "x", "symbol": "X"}]}'
        % datetime.now(timezone.utc).isoformat()
    )
    # Fresh cache → no network touched (base_url would fail if it were).
    out = build_universe(cache_path=str(cache), base_url="http://invalid.local")
    assert out == [{"id": "x", "symbol": "X"}]
