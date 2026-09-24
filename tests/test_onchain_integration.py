"""End-to-end: collectors write snapshots, then both consumers read them.

The whole point of the collector/reporter split is that the digest and the
signal gate see the *same* numbers from one fetch. These tests run the real
collectors against stubbed HTTP into a real StateStore, then assert that both
consumers agree — which no unit test of either half can show on its own.
"""

from __future__ import annotations

import re

from wolf.market import ContextProvider
from wolf.onchain import (
    CoinbasePremiumCollector,
    MacroFlowCollector,
    ValuationCollector,
    WhaleHyperliquidCollector,
)
from wolf.reports.flow import FlowIntelReporter
from wolf.screener import Screener
from wolf.detectors.base import SignalCandidate


def _plain(html: str) -> str:
    """Strip the Telegram markup so assertions read like the message does."""
    return re.sub(r"</?[bi]>", "", html)


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _Session:
    """One stub serving every endpoint the collectors touch."""

    def __init__(self, wallets: list[str], positions: dict) -> None:
        self.wallets = wallets
        self.positions = positions

    def get(self, url, params=None, timeout=None, headers=None):
        if "leaderboard" in url:
            return _Resp({"leaderboardRows": [
                {"ethAddress": a, "pnl": {"allTime": 100 - i}}
                for i, a in enumerate(self.wallets)
            ]})
        if "coins/markets" in url:
            return _Resp([{
                "market_cap": 90_000_000_000, "fully_diluted_valuation": 100_000_000_000,
                "total_volume": 20_000_000_000, "ath_change_percentage": -90.0,
            }])
        if "api.coinbase.com" in url:
            return _Resp({"data": {"amount": "100120"}})
        if "api.binance.com" in url:
            return _Resp({"price": "100000"})
        return _Resp(None)

    def post(self, url, json=None, timeout=None, headers=None):
        user = (json or {}).get("user", "")
        return _Resp({"assetPositions": self.positions.get(user, [])})


def _hl(coin: str, szi: float, px: float = 2_000.0) -> dict:
    return {"position": {"coin": coin, "szi": str(szi), "entryPx": str(px)}}


class _FundingClient:
    def get_funding_rate(self, symbol):
        return -0.06

    def get_open_interest_change(self, symbol):
        return 4.0


class _Universe:
    def symbols(self):
        return ["SOLUSDT", "BTCUSDT", "ETHUSDT"]


def _run_collectors(store) -> None:
    """Baseline scan, then a second scan in which three wallets pile into SOL."""
    wallets = ["0xa", "0xb", "0xc"]
    session = _Session(wallets, {})
    whale = WhaleHyperliquidCollector(store, session=session, request_pause=0)
    whale.scan()                                        # baseline
    session.positions = {a: [_hl("SOL", 100)] for a in wallets}
    whale.scan()                                        # coordination detected

    ValuationCollector(store, session=session).collect(["SOLUSDT"])
    CoinbasePremiumCollector(store, session=session).collect()


def test_collected_snapshots_reach_the_signal_context(store):
    _run_collectors(store)

    ctx = ContextProvider(_FundingClient(), store).build("SOLUSDT")

    assert ctx.whale_coordination == "LONG"
    assert ctx.whale_wallet_count == 3
    assert ctx.onchain_bias == "SUPPORTS_LONG"
    assert "VALUASI ON-CHAIN SOL" in ctx.onchain_brief
    assert ctx.funding_rate == -0.06, "derivatives data is unaffected"


def test_premium_reaches_btc_context_only(store):
    _run_collectors(store)

    assert ContextProvider(_FundingClient(), store).build("BTCUSDT").coinbase_premium_pct == 0.12
    assert ContextProvider(_FundingClient(), store).build("SOLUSDT").coinbase_premium_pct is None


def test_collected_snapshots_reach_the_digest(store):
    _run_collectors(store)
    MacroFlowCollector(store, coingecko=_StubCG(), defillama=_StubLlama()).collect()

    text = _plain(FlowIntelReporter(store, _Universe(), tz="UTC").build())

    assert "$SOL LONG — 3L / 0S" in text
    assert "+0.120%" in text
    assert "1/ MARKET MACRO" in text


def test_both_consumers_see_the_same_whale_reading(store):
    """One fetch, two consumers — the bug this architecture exists to prevent."""
    _run_collectors(store)
    MacroFlowCollector(store, coingecko=_StubCG(), defillama=_StubLlama()).collect()

    ctx = ContextProvider(_FundingClient(), store).build("SOLUSDT")
    text = _plain(FlowIntelReporter(store, _Universe(), tz="UTC").build())

    assert ctx.whale_coordination == "LONG"
    assert f"$SOL LONG — {ctx.whale_long_count}L / {ctx.whale_short_count}S" in text


def test_whale_gate_acts_on_collected_data(store, fake_client, tracker):
    """A SHORT signal on a coin three-plus whales just went long on."""
    wallets = [f"0x{i}" for i in range(6)]
    session = _Session(wallets, {})
    whale = WhaleHyperliquidCollector(store, session=session, request_pause=0)
    whale.scan()
    session.positions = {a: [_hl("SOL", 100)] for a in wallets}
    whale.scan()

    ctx = ContextProvider(_FundingClient(), store).build("SOLUSDT")
    screener = Screener(fake_client, tracker, [], whale_veto_min_wallets=5)
    candidate = SignalCandidate(
        symbol="SOLUSDT", signal_type="PREDUMP", direction="SHORT",
        entry_price=200.0, tp=180.0, sl=210.0, score=80,
        confluence_level="MEDIUM", reasons=[], strategy="PREDUMP",
    )

    assert ctx.whale_wallet_count == 6
    assert screener._whale_vetoed(candidate, ctx)


def test_everything_degrades_when_no_collector_has_run(store, fake_client, tracker):
    """The designed fallback: behave exactly as before the collectors existed."""
    ctx = ContextProvider(_FundingClient(), store).build("SOLUSDT")
    screener = Screener(fake_client, tracker, [])
    candidate = SignalCandidate(
        symbol="SOLUSDT", signal_type="MOMENTUM", direction="LONG",
        entry_price=200.0, tp=220.0, sl=190.0, score=80,
        confluence_level="MEDIUM", reasons=[], strategy="MOMENTUM",
    )

    assert ctx.funding_rate == -0.06, "candle/derivatives path is untouched"
    assert ctx.whale_coordination is None and ctx.onchain_bias is None
    assert not screener._whale_vetoed(candidate, ctx)
    assert FlowIntelReporter(store, _Universe()).build() is None


class _StubCG:
    def top_markets(self, limit=60):
        from wolf.flow.coingecko import TokenMetrics
        return [TokenMetrics(symbol="SOL", name="Solana", price=200.0, change_24h=3.0,
                             market_cap=90e9, fdv=100e9, volume_24h=20e9,
                             ath_change_pct=-90.0)]

    def global_data(self):
        from wolf.flow.coingecko import GlobalMetrics
        return GlobalMetrics(btc_dominance=54.0, total_market_cap=3.1e12,
                             market_cap_change_24h=1.5, usdt_dominance=4.9)


class _StubLlama:
    def chain_activity(self, chains=None):
        from wolf.flow.defillama import ChainActivity
        return [ChainActivity(chain="solana", dex_volume_24h=4e9, change_1d=10.0)]

    def stablecoin_supply(self):
        from wolf.flow.defillama import StablecoinSupply
        return StablecoinSupply(total_usd=180e9, change_1d_pct=0.3, change_7d_pct=1.2)
