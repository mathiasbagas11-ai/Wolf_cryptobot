"""Tests for market context and its effect on PREPUMP/PREDUMP."""

from __future__ import annotations

from wolf.detectors import PreDumpDetector, PrePumpDetector
from wolf.market import ContextProvider, MarketContext
from wolf.models import Candle


def _c(t, o, h, l, c, v=100.0):
    return Candle(time=t * 900_000, open=o, high=h, low=l, close=c, volume=v)


# ── MarketContext predicates ──────────────────────────────────────────────
def test_funding_predicates():
    assert MarketContext(funding_rate=-0.12).funding_extreme_squeeze
    assert MarketContext(funding_rate=-0.06).funding_squeeze
    assert not MarketContext(funding_rate=-0.06).funding_extreme_squeeze
    assert MarketContext(funding_rate=0.08).funding_overheated_long
    assert MarketContext(funding_rate=None).funding_squeeze is False


def test_oi_predicates():
    assert MarketContext(oi_change_pct=5.0).oi_rising
    assert MarketContext(oi_change_pct=-5.0).oi_falling
    assert MarketContext(oi_change_pct=0.5).oi_rising is False


# ── ContextProvider uses the client ───────────────────────────────────────
class _StubClient:
    def get_funding_rate(self, symbol):
        return -0.08

    def get_open_interest_change(self, symbol):
        return 3.5


def test_context_provider_builds_from_client():
    ctx = ContextProvider(_StubClient()).build("BTCUSDT")
    assert ctx.funding_rate == -0.08
    assert ctx.oi_change_pct == 3.5
    assert ctx.funding_squeeze


# ── Context raises the score (and is purely additive) ─────────────────────
def _prepump_candles():
    cs = []
    p = 90.0
    for i in range(41):
        p += 0.4
        cs.append(_c(i, p - 0.1, p + 0.3, p - 0.2, p, 100.0))
    base = cs[-1].close
    for k in range(18):
        cs.append(_c(41 + k, base, base + 0.25, base - 0.25, base + (0.05 if k % 2 else -0.05), 90.0))
    # Breakout candle above the consolidation high (required by the 0/8 fix).
    cs.append(_c(59, base, base + 1.6, base - 0.1, base + 1.3, 260.0))
    return cs


def test_prepump_funding_bonus_increases_score():
    cs = _prepump_candles()
    det = PrePumpDetector()
    base = det.evaluate("X", cs, None)
    boosted = det.evaluate("X", cs, MarketContext(funding_rate=-0.12, oi_change_pct=5.0))
    assert base is not None and boosted is not None
    assert boosted.score > base.score
    assert any("Funding extreme" in r for r in boosted.reasons)


def test_predump_funding_bonus_increases_score():
    cs = []
    p = 90.0
    for i in range(59):
        p += 0.5
        cs.append(_c(i, p - 0.2, p + 0.4, p - 0.4, p, 120.0 if i < 55 else 40.0))
    top = cs[-1].close
    cs.append(_c(59, top + 0.2, top + 2.5, top - 0.3, top - 0.5, 35.0))
    det = PreDumpDetector()
    base = det.evaluate("X", cs, None)
    boosted = det.evaluate("X", cs, MarketContext(funding_rate=0.09, oi_change_pct=-5.0))
    assert base is not None and boosted is not None
    assert boosted.score > base.score


# ── on-chain context: staleness, lookup, symbol normalisation ─────────────
from datetime import datetime, timedelta, timezone

from wolf.market import age_minutes, is_fresh, parse_iso
from wolf.state import StateStore


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _seed(store: StateStore, *, valuation_age=1.0, whale_age=1.0, premium_age=1.0) -> None:
    store.write("onchain_valuation", {
        "ts": _ts(valuation_age),
        "symbols": {"SOL": {"bias": "SUPPORTS_LONG", "brief": "VALUASI ON-CHAIN SOL"}},
    })
    store.write("whale_hyperliquid", {
        "ts": _ts(whale_age),
        # ``coins`` is the last window's entries; ``bias`` is where every tracked
        # wallet is sitting. The context reads ``bias`` — see _whale_for.
        "coins": {"SOL": {"direction": "SHORT", "wallet_count": 5, "notional_usd": 2_400_000}},
        "bias": {"SOL": {"long_count": 0, "short_count": 5,
                         "long_notional": 0.0, "short_notional": 2_400_000.0}},
    })
    store.write("coinbase_premium", {"ts": _ts(premium_age), "premium_pct": 0.12})


def _provider(store, **kw):
    return ContextProvider(_StubClient(), store, **kw)


# ── timestamp helpers ─────────────────────────────────────────────────────
def test_parse_iso_assumes_utc_when_naive():
    assert parse_iso("2026-01-01T00:00:00").tzinfo is timezone.utc


def test_parse_iso_rejects_junk():
    assert parse_iso("not-a-date") is None
    assert parse_iso("") is None
    assert parse_iso(None) is None
    assert parse_iso(12345) is None


def test_age_minutes():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert age_minutes("2026-01-01T11:30:00+00:00", now=now) == 30.0
    assert age_minutes("nope", now=now) is None


def test_is_fresh_boundaries():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert is_fresh({"ts": "2026-01-01T11:31:00+00:00"}, 30.0, now=now)
    assert is_fresh({"ts": "2026-01-01T11:30:00+00:00"}, 30.0, now=now), "exactly at the limit counts"
    assert not is_fresh({"ts": "2026-01-01T11:29:00+00:00"}, 30.0, now=now)


def test_undated_document_is_treated_as_stale():
    """An undated snapshot must not gate signals forever."""
    assert not is_fresh({"coins": {}}, 30.0)
    assert not is_fresh(None, 30.0)
    assert not is_fresh("garbage", 30.0)


# ── fresh data flows into the context ─────────────────────────────────────
def test_context_reads_fresh_onchain_state(store):
    _seed(store)
    ctx = _provider(store).build("SOLUSDT")

    assert ctx.onchain_bias == "SUPPORTS_LONG"
    assert "VALUASI ON-CHAIN SOL" in ctx.onchain_brief
    assert ctx.whale_coordination == "SHORT"
    assert ctx.whale_wallet_count == 5


def test_context_still_carries_derivatives_data(store):
    _seed(store)
    ctx = _provider(store).build("SOLUSDT")
    assert ctx.funding_rate == -0.08
    assert ctx.oi_change_pct == 3.5


# ── staleness → None ──────────────────────────────────────────────────────
def test_stale_valuation_reads_as_absent(store):
    _seed(store, valuation_age=45.0)
    ctx = _provider(store).build("SOLUSDT")

    assert ctx.onchain_bias is None
    assert ctx.onchain_brief == ""
    assert ctx.whale_coordination == "SHORT", "fresh sources are unaffected"


def test_stale_whale_data_reads_as_absent(store):
    _seed(store, whale_age=45.0)
    ctx = _provider(store).build("SOLUSDT")

    assert ctx.whale_coordination is None
    assert ctx.whale_wallet_count == 0


def test_stale_premium_reads_as_absent(store):
    _seed(store, premium_age=90.0)
    assert _provider(store).build("BTCUSDT").coinbase_premium_pct is None


def test_staleness_threshold_is_configurable(store):
    _seed(store, whale_age=45.0)
    assert _provider(store, staleness_min=60.0).build("SOLUSDT").whale_coordination == "SHORT"


def test_missing_store_degrades_to_derivatives_only():
    ctx = ContextProvider(_StubClient()).build("SOLUSDT")
    assert ctx.funding_rate == -0.08
    assert ctx.onchain_bias is None
    assert ctx.whale_coordination is None
    assert ctx.coinbase_premium_pct is None


def test_empty_store_degrades_cleanly(store):
    ctx = _provider(store).build("SOLUSDT")
    assert ctx.onchain_bias is None
    assert ctx.whale_wallet_count == 0


def test_corrupt_state_rows_do_not_raise(store):
    store.write("whale_hyperliquid", {"ts": _ts(1.0), "bias": {"SOL": "not-a-dict"}})
    store.write("onchain_valuation", {"ts": _ts(1.0), "symbols": "nope"})
    ctx = _provider(store).build("SOLUSDT")
    assert ctx.whale_coordination is None
    assert ctx.onchain_bias is None


def test_non_numeric_wallet_count_falls_back_to_zero(store):
    store.write("whale_hyperliquid", {
        "ts": _ts(1.0), "bias": {"SOL": {"long_count": "many", "short_count": None}},
    })
    assert _provider(store).build("SOLUSDT").whale_wallet_count == 0


# ── symbol normalisation ──────────────────────────────────────────────────
def test_context_normalises_symbol_via_split_quote(store):
    _seed(store)
    assert _provider(store).build("SOLUSDT").whale_coordination == "SHORT"
    assert _provider(store).build("SOL").whale_coordination == "SHORT"


def test_normalisation_does_not_gut_names_containing_the_quote():
    """``.replace('USDT', '')`` mapped SUSDTUSDT to 'S' and looked up the wrong coin."""
    assert ContextProvider.base_symbol("SUSDTUSDT") == "SUSDT"
    assert ContextProvider.base_symbol("BTCUSDT") == "BTC"
    assert ContextProvider.base_symbol("ethusdt") == "ETH"


def test_unknown_symbol_gets_no_whale_data(store):
    _seed(store)
    ctx = _provider(store).build("DOGEUSDT")
    assert ctx.whale_coordination is None
    assert ctx.onchain_bias is None


# ── Coinbase premium is BTC-scoped ────────────────────────────────────────
def test_premium_applies_to_btc(store):
    _seed(store)
    assert _provider(store).build("BTCUSDT").coinbase_premium_pct == 0.12


def test_premium_is_none_for_every_other_symbol(store):
    _seed(store)
    assert _provider(store).build("SOLUSDT").coinbase_premium_pct is None
    assert _provider(store).build("ETHUSDT").coinbase_premium_pct is None


# ── whales_oppose ─────────────────────────────────────────────────────────
def test_whales_oppose_requires_opposite_direction_and_enough_wallets():
    ctx = MarketContext(whale_coordination="SHORT", whale_wallet_count=5)
    assert ctx.whales_oppose("LONG", min_wallets=5)
    assert not ctx.whales_oppose("SHORT", min_wallets=5), "same direction is agreement"
    assert not ctx.whales_oppose("LONG", min_wallets=6), "below the threshold"


def test_whales_oppose_is_false_without_data():
    assert not MarketContext().whales_oppose("LONG", min_wallets=3)
    assert not MarketContext(whale_coordination="SHORT", whale_wallet_count=5).whales_oppose("", 3)


# ── positioning outlives the entry window ─────────────────────────────────
def test_context_reads_positioning_not_the_last_window_of_entries(store):
    """The bug this fixes: ``coins`` empties one scan after detection, and a
    gate reading it went blind while the same whales were still holding."""
    store.write("whale_hyperliquid", {
        "ts": _ts(1.0),
        "coins": {},                      # nobody moved in the last window
        "bias": {"SOL": {"long_count": 6, "short_count": 0,
                         "long_notional": 1_200_000.0, "short_notional": 0.0}},
    })
    ctx = _provider(store).build("SOLUSDT")

    assert ctx.whale_coordination == "LONG"
    assert ctx.whale_wallet_count == 6


def test_wallet_count_is_net_dominance():
    """Six against one is a lean; six against five is a market disagreeing."""
    store_rows = [
        ({"long_count": 6, "short_count": 1}, "LONG", 5),
        ({"long_count": 6, "short_count": 5}, "LONG", 1),
        ({"long_count": 1, "short_count": 7}, "SHORT", 6),
    ]
    for row, direction, net in store_rows:
        got = ContextProvider._whale_for({"bias": {"SOL": row}}, "SOL")
        assert got[0] == direction and got[1] == net


def test_balanced_book_is_not_a_direction():
    assert ContextProvider._whale_for(
        {"bias": {"SOL": {"long_count": 4, "short_count": 4}}}, "SOL"
    ) == (None, 0, 4, 4)


def test_context_exposes_raw_counts_for_display(store):
    store.write("whale_hyperliquid", {
        "ts": _ts(1.0),
        "bias": {"SOL": {"long_count": 6, "short_count": 2}},
    })
    ctx = _provider(store).build("SOLUSDT")

    assert (ctx.whale_long_count, ctx.whale_short_count) == (6, 2)
    assert ctx.whale_wallet_count == 4, "net, not raw"


def test_near_balanced_book_cannot_veto(store):
    store.write("whale_hyperliquid", {
        "ts": _ts(1.0),
        "bias": {"SOL": {"long_count": 6, "short_count": 5}},
    })
    ctx = _provider(store).build("SOLUSDT")

    assert not ctx.whales_oppose("SHORT", min_wallets=5)
