"""Tests for the on-chain valuation collector.

The judgement (compute → assess → brief) is pure, so everything below runs on
canned payloads with no network. The collector itself is exercised through a
stub session so caching, 429 backoff and symbol normalisation are covered too.
"""

from __future__ import annotations

from wolf.onchain.valuation import (
    STATE_KEY,
    ValuationCollector,
    assess_valuation,
    base_symbol,
    build_valuation_brief,
    compute_valuation_metrics,
    parse_markets_row,
    parse_tvl,
)


# ── compute_valuation_metrics ─────────────────────────────────────────────
def test_compute_metrics_basic_ratios():
    m = compute_valuation_metrics({
        "market_cap": 500_000_000,
        "fully_diluted_valuation": 1_000_000_000,
        "total_volume": 100_000_000,
        "circulating_supply": 500_000,
        "total_supply": 1_000_000,
        "ath_change_percentage": -60.0,
    })
    assert m["fdv_ratio"] == 0.5
    assert m["vol_mcap"] == 0.2
    assert m["circ_pct"] == 50.0
    assert m["ath_chg_pct"] == -60.0


def test_compute_metrics_missing_denominators_are_none_not_zero():
    """A missing denominator must read as 'no data', not as a real 0.0."""
    m = compute_valuation_metrics({"market_cap": 0, "fully_diluted_valuation": 0})
    assert m["fdv_ratio"] is None
    assert m["vol_mcap"] is None
    assert m["circ_pct"] is None
    assert m["ath_chg_pct"] is None


def test_compute_metrics_handles_empty_and_none_payload():
    for payload in (None, {}):
        m = compute_valuation_metrics(payload)
        assert m["mcap"] == 0.0
        assert m["fdv_ratio"] is None


def test_compute_metrics_adds_tvl_dimension():
    m = compute_valuation_metrics(
        {"market_cap": 300_000_000},
        {"tvl": 600_000_000, "tvl_chg_30d": 22.0},
    )
    assert m["tvl"] == 600_000_000
    assert m["mcap_tvl"] == 0.5
    assert m["tvl_chg_30d"] == 22.0


def test_compute_metrics_ignores_zero_tvl():
    m = compute_valuation_metrics({"market_cap": 1_000_000}, {"tvl": 0})
    assert m["tvl"] is None
    assert m["mcap_tvl"] is None


# ── assess_valuation ──────────────────────────────────────────────────────
def test_assess_directionless_reports_what_fundamentals_say():
    """No trade attached → SUPPORTS_*, never CAUTION."""
    metrics = compute_valuation_metrics({
        "market_cap": 900_000_000,
        "fully_diluted_valuation": 1_000_000_000,   # 90% circulating → bull
        "total_volume": 200_000_000,                # turnover 0.22 → bull
        "ath_change_percentage": -90.0,             # deep below ATH → bull
    })
    a = assess_valuation(metrics)
    assert a["bias"] == "SUPPORTS_LONG"
    assert len(a["bull_notes"]) == 3
    assert a["bear_notes"] == []


def test_assess_bearish_metrics_supports_short():
    metrics = compute_valuation_metrics({
        "market_cap": 200_000_000,
        "fully_diluted_valuation": 2_000_000_000,   # 10% circulating → bear
        "total_volume": 1_000_000,                  # turnover 0.005 → bear
        "ath_change_percentage": -2.0,              # at ATH → bear
    })
    a = assess_valuation(metrics)
    assert a["bias"] == "SUPPORTS_SHORT"
    assert len(a["bear_notes"]) == 3


def test_assess_against_trade_direction_flags_caution():
    """Same metrics, judged against a LONG, become CAUTION."""
    metrics = compute_valuation_metrics({
        "market_cap": 200_000_000,
        "fully_diluted_valuation": 2_000_000_000,
        "total_volume": 1_000_000,
        "ath_change_percentage": -2.0,
    })
    assert assess_valuation(metrics, "LONG")["bias"] == "CAUTION"
    assert assess_valuation(metrics, "SHORT")["bias"] == "SUPPORTS_SHORT"


def test_assess_aliases_pump_directions_to_long():
    metrics = compute_valuation_metrics({
        "market_cap": 200_000_000,
        "fully_diluted_valuation": 2_000_000_000,
        "total_volume": 1_000_000,
    })
    assert assess_valuation(metrics, "PREPUMP")["bias"] == "CAUTION"


def test_assess_no_signal_is_neutral():
    a = assess_valuation(compute_valuation_metrics({}))
    assert a["bias"] == "NEUTRAL"
    assert a["headline"] == "Fundamental netral"


def test_assess_balanced_notes_are_neutral_in_both_directions():
    metrics = compute_valuation_metrics({
        "market_cap": 900_000_000,
        "fully_diluted_valuation": 1_000_000_000,   # bull
        "total_volume": 1_000_000,                  # bear (turnover 0.001)
    })
    a = assess_valuation(metrics, "LONG")
    assert len(a["bull_notes"]) == 1 and len(a["bear_notes"]) == 1
    assert a["bias"] == "NEUTRAL"


def test_assess_tvl_notes():
    cheap = assess_valuation({"mcap_tvl": 0.4, "tvl_chg_30d": 30.0})
    assert cheap["bias"] == "SUPPORTS_LONG"
    rich = assess_valuation({"mcap_tvl": 12.0, "tvl_chg_30d": -40.0})
    assert rich["bias"] == "SUPPORTS_SHORT"


# ── build_valuation_brief ─────────────────────────────────────────────────
def test_brief_contains_headline_and_facts():
    metrics = compute_valuation_metrics({
        "market_cap": 500_000_000,
        "fully_diluted_valuation": 1_000_000_000,
        "total_volume": 100_000_000,
        "ath_change_percentage": -60.0,
    })
    brief = build_valuation_brief(metrics, assess_valuation(metrics), "SOL")
    assert "VALUASI ON-CHAIN SOL" in brief
    assert "FDV ratio 0.50" in brief
    assert "MCap $500M" in brief


def test_brief_empty_without_assessment():
    assert build_valuation_brief({}, {}, "BTC") == ""


# ── payload parsing ───────────────────────────────────────────────────────
def test_parse_markets_row():
    assert parse_markets_row([{"id": "solana"}]) == {"id": "solana"}
    assert parse_markets_row([]) is None
    assert parse_markets_row({"error": "x"}) is None
    assert parse_markets_row(None) is None


def test_parse_tvl_series_and_30d_change():
    series = [{"totalLiquidityUSD": 100.0} for _ in range(29)] + [{"totalLiquidityUSD": 200.0}]
    parsed = parse_tvl({"tvl": series})
    assert parsed["tvl"] == 200.0
    assert parsed["tvl_chg_30d"] == 100.0


def test_parse_tvl_falls_back_to_chain_breakdown():
    parsed = parse_tvl({"currentChainTvls": {"ethereum": 10.0, "base": 5.0}})
    assert parsed["tvl"] == 15.0
    assert parsed["tvl_chg_30d"] is None


def test_parse_tvl_rejects_junk():
    assert parse_tvl(None) is None
    assert parse_tvl({"tvl": []}) is None


# ── symbol normalisation ──────────────────────────────────────────────────
def test_base_symbol_strips_quote():
    assert base_symbol("BTCUSDT") == "BTC"
    assert base_symbol("solusdt") == "SOL"
    assert base_symbol("ETH") == "ETH"


def test_base_symbol_does_not_gut_names_containing_the_quote():
    """The old ``.replace('USDT', '')`` turned SUSDTUSDT into S."""
    assert base_symbol("SUSDTUSDT") == "SUSDT"


# ── collector: caching, backoff, persistence ──────────────────────────────
class _StubResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _StubSession:
    """Records every GET and answers from a url-substring → payload map."""

    def __init__(self, routes: dict, status: int = 200) -> None:
        self.routes = routes
        self.status = status
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if self.status != 200:
            return _StubResponse(None, self.status)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return _StubResponse(payload)
        return _StubResponse(None, 404)


_MARKETS = [{
    "market_cap": 900_000_000,
    "fully_diluted_valuation": 1_000_000_000,
    "total_volume": 200_000_000,
    "ath_change_percentage": -90.0,
}]


def test_collector_writes_snapshot_to_store(store):
    session = _StubSession({"/coins/markets": _MARKETS})
    collector = ValuationCollector(store, session=session)

    doc = collector.collect(["BTCUSDT", "ETHUSDT"])

    assert set(doc["symbols"]) == {"BTC", "ETH"}
    assert doc["symbols"]["BTC"]["bias"] == "SUPPORTS_LONG"
    assert "ts" in doc
    assert store.read(STATE_KEY)["symbols"]["ETH"]["brief"]


def test_collector_caches_within_ttl(store):
    session = _StubSession({"/coins/markets": _MARKETS})
    collector = ValuationCollector(store, session=session)

    collector.collect(["BTCUSDT"])
    calls_after_first = len(session.calls)
    collector.collect(["BTCUSDT"])

    assert len(session.calls) == calls_after_first, "second cycle must be served from cache"


def test_collector_refetches_after_ttl_expiry(store):
    session = _StubSession({"/coins/markets": _MARKETS})
    collector = ValuationCollector(store, session=session, cache_ttl=0.0)

    collector.collect(["BTCUSDT"])
    first = len(session.calls)
    collector.collect(["BTCUSDT"])

    assert len(session.calls) > first


def test_collector_cache_is_per_instance_not_global(store):
    """The old module-level ``_CACHE`` leaked between collectors and tests."""
    session_a = _StubSession({"/coins/markets": _MARKETS})
    session_b = _StubSession({"/coins/markets": _MARKETS})
    ValuationCollector(store, session=session_a).collect(["BTCUSDT"])
    ValuationCollector(store, session=session_b).collect(["BTCUSDT"])

    assert session_b.calls, "a fresh collector must not inherit another's cache"


def test_collector_backs_off_on_429(store):
    session = _StubSession({}, status=429)
    collector = ValuationCollector(store, session=session, rate_limit_backoff=600.0)

    doc = collector.collect(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    assert collector.rate_limited
    assert doc["rate_limited"] is True
    assert doc["symbols"] == {}
    # One call trips the backoff; the rest are skipped without touching HTTP.
    assert len(session.calls) == 1


def test_collector_caches_misses_to_protect_the_rate_limit(store):
    session = _StubSession({"/coins/markets": []})
    collector = ValuationCollector(store, session=session)

    collector.collect(["BTCUSDT"])
    first = len(session.calls)
    collector.collect(["BTCUSDT"])

    assert len(session.calls) == first, "an unlisted symbol must not be re-requested"


def test_collector_resolves_unknown_symbol_via_search_once(store):
    session = _StubSession({
        "/search": {"coins": [{"symbol": "ZZZ", "id": "zzz-token"}]},
        "/coins/markets": _MARKETS,
    })
    collector = ValuationCollector(store, session=session, cache_ttl=0.0)

    assert collector.coin_id("ZZZUSDT") == "zzz-token"
    collector.coin_id("ZZZUSDT")

    assert sum(1 for c in session.calls if "/search" in c) == 1


def test_collector_uses_override_map_without_search(store):
    session = _StubSession({"/coins/markets": _MARKETS})
    collector = ValuationCollector(store, session=session)

    assert collector.coin_id("BTCUSDT") == "bitcoin"
    assert not any("/search" in c for c in session.calls)
