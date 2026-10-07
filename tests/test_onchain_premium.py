"""Tests for the Coinbase premium collector and the macro-flow collector."""

from __future__ import annotations

from wolf.onchain.coinbase_premium import (
    STATE_KEY,
    CoinbasePremiumCollector,
    classify_premium,
    compute_premium_pct,
    parse_binance_price,
    parse_coinbase_spot,
    parse_coinbase_ticker,
)
from wolf.onchain.macro import STATE_KEY as MACRO_KEY
from wolf.onchain.macro import MacroFlowCollector


# ── compute_premium_pct ───────────────────────────────────────────────────
def test_premium_is_the_spread_in_percent():
    assert round(compute_premium_pct(100_100.0, 100_000.0), 4) == 0.1


def test_negative_premium_when_coinbase_trades_below_binance():
    assert round(compute_premium_pct(99_900.0, 100_000.0), 4) == -0.1


def test_premium_is_none_when_either_leg_is_missing():
    assert compute_premium_pct(None, 100_000.0) is None
    assert compute_premium_pct(100_000.0, None) is None
    assert compute_premium_pct(100_000.0, 0.0) is None


# ── classify_premium ──────────────────────────────────────────────────────
def test_classify_premium_thresholds():
    assert classify_premium(0.20) == "ACCUMULATION"
    assert classify_premium(0.05) == "ACCUMULATION"
    assert classify_premium(0.04) == "NEUTRAL"
    assert classify_premium(0.0) == "NEUTRAL"
    assert classify_premium(-0.04) == "NEUTRAL"
    assert classify_premium(-0.05) == "DISTRIBUTION"


def test_classify_premium_without_data_is_neutral():
    assert classify_premium(None) == "NEUTRAL"


# ── payload parsing ───────────────────────────────────────────────────────
def test_parse_coinbase_spot():
    assert parse_coinbase_spot({"data": {"amount": "101000.55"}}) == 101_000.55
    assert parse_coinbase_spot({"data": {"amount": "0"}}) is None
    assert parse_coinbase_spot({"data": "nope"}) is None
    assert parse_coinbase_spot(None) is None


def test_parse_coinbase_ticker_and_binance():
    assert parse_coinbase_ticker({"price": "99000"}) == 99_000.0
    assert parse_binance_price({"price": "99000"}) == 99_000.0
    assert parse_binance_price({"price": "bad"}) is None
    assert parse_binance_price([]) is None


# ── collector ─────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("boom")

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return _Resp(payload)
        return _Resp(None, 404)


def test_collector_writes_premium_snapshot(store):
    session = _StubSession({
        "api.coinbase.com": {"data": {"amount": "100100"}},
        "api.binance.com": {"price": "100000"},
    })

    doc = CoinbasePremiumCollector(store, session=session).collect()

    assert doc["symbol"] == "BTC"
    assert doc["premium_pct"] == 0.1
    assert doc["signal"] == "ACCUMULATION"
    assert doc["available"] is True
    assert store.read(STATE_KEY)["premium_pct"] == 0.1


def test_collector_falls_back_to_the_exchange_ticker(store):
    session = _StubSession({
        "api.exchange.coinbase.com": {"price": "99900"},
        "api.binance.com": {"price": "100000"},
    })

    doc = CoinbasePremiumCollector(store, session=session).collect()

    assert doc["signal"] == "DISTRIBUTION"
    assert doc["coinbase_price"] == 99_900.0


def test_collector_records_unavailable_rather_than_a_fake_zero(store):
    """No data must not read as 'the premium is exactly 0.00%'."""
    session = _StubSession({"api.binance.com": {"price": "100000"}})

    doc = CoinbasePremiumCollector(store, session=session).collect()

    assert doc["premium_pct"] is None
    assert doc["available"] is False
    assert doc["signal"] == "NEUTRAL"


def test_collector_is_btc_scoped():
    assert CoinbasePremiumCollector.symbol == "BTC"


# ── macro collector ───────────────────────────────────────────────────────
class _StubCG:
    def top_markets(self, limit=60):
        from wolf.flow.coingecko import TokenMetrics
        return [TokenMetrics(symbol="SOL", name="Solana", price=200.0, change_24h=3.0,
                             market_cap=90e9, fdv=110e9, volume_24h=5e9,
                             ath_change_pct=-25.0)]

    def global_data(self):
        from wolf.flow.coingecko import GlobalMetrics
        return GlobalMetrics(btc_dominance=54.2, total_market_cap=3.1e12,
                             market_cap_change_24h=1.4, usdt_dominance=4.8)


class _StubLlama:
    def chain_activity(self, chains=None):
        from wolf.flow.defillama import ChainActivity
        return [ChainActivity(chain="solana", dex_volume_24h=4.2e9, change_1d=12.0)]

    def stablecoin_supply(self):
        from wolf.flow.defillama import StablecoinSupply
        return StablecoinSupply(total_usd=180e9, change_1d_pct=0.2, change_7d_pct=1.1)


class _DeadCG(_StubCG):
    def global_data(self):
        return None


class _DeadLlama(_StubLlama):
    def stablecoin_supply(self):
        return None


def test_macro_collector_writes_every_section(store):
    doc = MacroFlowCollector(store, coingecko=_StubCG(), defillama=_StubLlama()).collect()

    assert doc["global"]["btc_dominance"] == 54.2
    assert doc["stablecoin"]["change_7d_pct"] == 1.1
    assert doc["chains"][0]["label"] == "Solana"
    assert doc["markets"][0]["symbol"] == "SOL"
    assert "ts" in doc
    assert store.read(MACRO_KEY)["global"]["usdt_dominance"] == 4.8


def test_macro_collector_degrades_per_source(store):
    """One dead source must not cost the sections the others feed."""
    doc = MacroFlowCollector(store, coingecko=_DeadCG(), defillama=_DeadLlama()).collect()

    assert doc["global"] is None
    assert doc["stablecoin"] is None
    assert doc["chains"], "chain rotation still available"
    assert doc["markets"], "token screen still available"
