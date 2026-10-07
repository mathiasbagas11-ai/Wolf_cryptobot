"""Tests for the composite market-regime layer (Phase 1: data + context)."""

from __future__ import annotations

from wolf.flow.coingecko import CoinGeckoClient, GlobalMetrics
from wolf.flow.defillama import ChainActivity, StablecoinSupply
from wolf.flow.sentiment import FearGreed
from wolf.regime import UNKNOWN
from wolf.regime_composite import (
    CF_RISK_OFF,
    CF_RISK_ON,
    DP_BUILDING,
    DP_OUTFLOW,
    EXTREME_FEAR,
    FEAR,
    MarketContext,
    UD_NEUTRAL,
    UD_REVERSAL_RISK,
    UD_RISK_OFF,
    UD_RISK_ON,
    CompositeRegimeProvider,
    classify_chain_flow,
    classify_dry_powder,
    classify_sentiment,
    classify_usdt_d,
    pct_change_24h,
    percentile_rank,
)


# ── pure classifiers ─────────────────────────────────────────────────────────
def test_classify_sentiment_buckets():
    assert classify_sentiment(None) == UNKNOWN
    assert classify_sentiment(20) == EXTREME_FEAR
    assert classify_sentiment(25) == EXTREME_FEAR         # boundary inclusive
    assert classify_sentiment(40) == FEAR
    assert classify_sentiment(50) == "SENT_NEUTRAL"


def test_classify_dry_powder():
    assert classify_dry_powder(None) == UNKNOWN
    assert classify_dry_powder(-0.9) == DP_OUTFLOW        # supply shrinking = risk-off
    assert classify_dry_powder(0.9) == DP_BUILDING
    assert classify_dry_powder(0.1) == "DP_STABLE"


def test_classify_chain_flow():
    assert classify_chain_flow(None, None) == UNKNOWN
    assert classify_chain_flow(1.0, 2.0) == CF_RISK_ON
    assert classify_chain_flow(-1.0, -2.0) == CF_RISK_OFF
    assert classify_chain_flow(1.0, -2.0) == "CF_MIXED"


def test_classify_usdt_d_reversal_takes_precedence():
    # Extreme-high percentile → reversal risk even if 24h change looks risk-off.
    assert classify_usdt_d(change_24h=0.5, percentile=90.0) == UD_REVERSAL_RISK


def test_classify_usdt_d_bidirectional():
    assert classify_usdt_d(0.3, 50.0, change_threshold_pct=0.2) == UD_RISK_OFF   # rising
    assert classify_usdt_d(-0.3, 50.0, change_threshold_pct=0.2) == UD_RISK_ON   # falling
    assert classify_usdt_d(0.1, 50.0, change_threshold_pct=0.2) == UD_NEUTRAL    # flat
    assert classify_usdt_d(None, 50.0) == UNKNOWN                                # no history


# ── history helpers ──────────────────────────────────────────────────────────
def test_pct_change_24h_picks_nearest_sample():
    now = 1_000_000.0
    history = [
        {"ts": now - 24 * 3600, "value": 5.0},   # exactly 24h ago
        {"ts": now - 1 * 3600, "value": 5.9},     # recent, ignored
    ]
    change = pct_change_24h(history, current=5.5, now_ts=now)
    assert change == (5.5 - 5.0) / 5.0 * 100      # +10%


def test_pct_change_24h_none_when_no_sample_in_window():
    now = 1_000_000.0
    history = [{"ts": now - 1 * 3600, "value": 5.0}]  # only 1h old, no ~24h sample
    assert pct_change_24h(history, current=5.5, now_ts=now) is None


def test_percentile_rank():
    assert percentile_rank([], 5.0) is None
    assert percentile_rank([1, 2, 3, 4], 5) == 100.0
    assert percentile_rank([1, 2, 3, 4], 2) == 50.0


# ── MarketContext predicates ─────────────────────────────────────────────────
def test_short_reversal_risk_predicate():
    assert MarketContext(sentiment=EXTREME_FEAR).short_reversal_risk is True
    assert MarketContext(usdt_d=UD_RISK_ON).short_reversal_risk is True
    assert MarketContext(usdt_d=UD_REVERSAL_RISK).short_reversal_risk is True
    assert MarketContext(usdt_d=UD_RISK_OFF, sentiment=FEAR).short_reversal_risk is False


def test_short_risk_off_predicate():
    assert MarketContext(usdt_d=UD_RISK_OFF).short_risk_off is True
    assert MarketContext(dry_powder=DP_OUTFLOW).short_risk_off is True
    assert MarketContext().short_risk_off is False


# ── coingecko usdt.d parsing ─────────────────────────────────────────────────
def test_parse_global_reads_usdt_dominance():
    payload = {"data": {
        "market_cap_percentage": {"btc": 54.0, "usdt": 4.7},
        "total_market_cap": {"usd": 2.5e12},
        "market_cap_change_percentage_24h_usd": -1.2,
    }}
    g = CoinGeckoClient.parse_global(payload)
    assert g is not None
    assert g.usdt_dominance == 4.7
    assert g.btc_dominance == 54.0


# ── provider: fakes ──────────────────────────────────────────────────────────
class _FakeTrend:
    def __init__(self, bias="BULLISH", raise_=False):
        self._bias, self._raise = bias, raise_
        self.calls = 0

    def bias(self):
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        return self._bias


class _FakeSentiment:
    def __init__(self, fg=None, raise_=False):
        self._fg, self._raise = fg, raise_
        self.calls = 0

    def fear_greed(self):
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        return self._fg


class _FakeGecko:
    def __init__(self, g=None):
        self._g = g
        self.calls = 0

    def global_data(self):
        self.calls += 1
        return self._g


class _FakeLlama:
    def __init__(self, stable=None, chains=None):
        self._stable, self._chains = stable, chains or []

    def stablecoin_supply(self):
        return self._stable

    def chain_activity(self):
        return self._chains


def _clock(t):
    return lambda: t[0]


# ── provider behaviour ───────────────────────────────────────────────────────
def test_snapshot_combines_fresh_trend_with_flow(store):
    t = [1_000_000.0]
    gecko = _FakeGecko(GlobalMetrics(54.0, 2.5e12, -1.0, usdt_dominance=4.7))
    provider = CompositeRegimeProvider(
        _FakeTrend("BEARISH"),
        sentiment_client=_FakeSentiment(FearGreed(18, "Extreme Fear")),
        coingecko_client=gecko,
        defillama_client=_FakeLlama(StablecoinSupply(1e11, -0.9, -1.0), [ChainActivity("bsc", 1e9, -3.0)]),
        store=store,
        clock=_clock(t),
    )
    ctx = provider.snapshot()
    assert ctx.trend == "BEARISH"
    assert ctx.sentiment == EXTREME_FEAR
    assert ctx.dry_powder == DP_OUTFLOW
    assert ctx.chain_flow == CF_RISK_OFF
    assert ctx.short_reversal_risk is True   # extreme fear


def test_flow_dims_cached_within_ttl_trend_stays_fresh(store):
    t = [1_000_000.0]
    trend = _FakeTrend("BULLISH")
    sentiment = _FakeSentiment(FearGreed(50, "Neutral"))
    provider = CompositeRegimeProvider(
        trend, sentiment_client=sentiment, store=store, ttl_min=30, clock=_clock(t),
    )
    provider.snapshot()
    provider.snapshot()
    # Trend fetched every snapshot; flow (sentiment) fetched once inside the TTL.
    assert trend.calls == 2
    assert sentiment.calls == 1
    # After the TTL elapses, flow refetches.
    t[0] += 31 * 60
    provider.snapshot()
    assert sentiment.calls == 2


def test_fail_open_on_fetch_errors(store):
    t = [1_000_000.0]
    provider = CompositeRegimeProvider(
        _FakeTrend(raise_=True),
        sentiment_client=_FakeSentiment(raise_=True),
        store=store,
        clock=_clock(t),
    )
    ctx = provider.snapshot()
    assert ctx.trend == UNKNOWN
    assert ctx.sentiment == UNKNOWN
    assert ctx.short_reversal_risk is False   # UNKNOWN never scales


def test_usdt_d_cold_start_unknown_then_persists_history(store):
    t = [1_000_000.0]
    gecko = _FakeGecko(GlobalMetrics(54.0, 2.5e12, 0.5, usdt_dominance=4.7))
    provider = CompositeRegimeProvider(
        _FakeTrend("NEUTRAL"), coingecko_client=gecko, store=store, clock=_clock(t),
    )
    ctx = provider.snapshot()
    # No history yet → no 24h change, no percentile → UNKNOWN (fail-open).
    assert ctx.usdt_d == UNKNOWN
    assert ctx.usdtd_value == 4.7
    # The current reading is persisted for future percentile/change math.
    hist = store.read("usdtd_history", default=[])
    assert len(hist) == 1 and hist[0]["value"] == 4.7


def test_usdt_d_risk_off_from_seeded_history(store):
    now = 1_000_000.0
    t = [now]
    # Seed a value ~24h ago so the 24h change is computable and rising >0.2%.
    store.write("usdtd_history", [{"ts": now - 24 * 3600, "value": 4.0}])
    gecko = _FakeGecko(GlobalMetrics(54.0, 2.5e12, -1.0, usdt_dominance=4.5))  # +12.5%
    provider = CompositeRegimeProvider(
        _FakeTrend("BEARISH"), coingecko_client=gecko, store=store,
        usdtd_change_pct=0.2, clock=_clock(t),
    )
    ctx = provider.snapshot()
    assert ctx.usdt_d == UD_RISK_OFF
    assert ctx.usdtd_change_24h is not None and ctx.usdtd_change_24h > 0.2
