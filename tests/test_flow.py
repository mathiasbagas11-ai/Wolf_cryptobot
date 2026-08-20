"""Tests for the flow data layer: source parsing, token deep-dive, narrator.

The periodic Flow Intelligence digest moved to a StateStore-reading reporter and
is covered by ``tests/test_flow_intel.py``; what remains here is the shared HTTP
parsing and the on-demand single-token deep dive.
"""

from __future__ import annotations

from wolf.ai.base import LLMClient
from wolf.ai.openai_compat import OpenAICompatLLMClient
from wolf.config import TelegramSettings
from wolf.flow.coingecko import CoinGeckoClient, GlobalMetrics, TokenMetrics
from wolf.flow.defillama import ChainActivity, DefiLlamaClient, StablecoinSupply
from wolf.notify import TelegramNotifier
from wolf.reports import TokenDeepDive

from tests.test_telegram import FakeSession


# ── CoinGecko parsing ──────────────────────────────────────────────────────
def test_coingecko_parse_markets():
    payload = [
        {"symbol": "aero", "name": "Aerodrome", "current_price": 0.43,
         "price_change_percentage_24h": 14.7, "market_cap": 300_000_000,
         "fully_diluted_valuation": 330_000_000, "total_volume": 60_000_000,
         "ath_change_percentage": -62.5},
    ]
    items = CoinGeckoClient.parse_markets(payload)
    assert items[0].symbol == "AERO"
    assert round(items[0].fdv_mc, 1) == 1.1
    assert round(items[0].vol_mc, 1) == 0.2
    assert items[0].ath_change_pct == -62.5


def test_coingecko_parse_markets_bad_input():
    assert CoinGeckoClient.parse_markets(None) == []
    assert CoinGeckoClient.parse_markets({"x": 1}) == []


def test_coingecko_fdv_mc_none_when_missing():
    t = TokenMetrics("X", "X", 1, 0, market_cap=0, fdv=0, volume_24h=0)
    assert t.fdv_mc is None and t.vol_mc is None


def test_coingecko_parse_global():
    payload = {"data": {"market_cap_percentage": {"btc": 54.2},
                        "total_market_cap": {"usd": 2.5e12},
                        "market_cap_change_percentage_24h_usd": 1.8}}
    g = CoinGeckoClient.parse_global(payload)
    assert g.btc_dominance == 54.2 and g.market_cap_change_24h == 1.8


# ── DefiLlama parsing ──────────────────────────────────────────────────────
def test_defillama_parse_chain():
    c = DefiLlamaClient.parse_chain("bsc", {"total24h": 1.2e9, "change_1d": 35.0})
    assert c.label == "BNB" and c.change_1d == 35.0
    assert DefiLlamaClient.parse_chain("bsc", {}) is None


def test_defillama_parse_stablecoins():
    rows = [{"totalCirculatingUSD": {"peggedUSD": 100}} for _ in range(8)]
    rows[-1] = {"totalCirculatingUSD": {"peggedUSD": 110}}
    s = DefiLlamaClient.parse_stablecoins(rows)
    assert s.total_usd == 110
    assert round(s.change_7d_pct, 0) == 10  # 100 -> 110


def test_defillama_parse_stablecoins_too_short():
    assert DefiLlamaClient.parse_stablecoins([{"x": 1}]) is None


# ── brief framework filter ─────────────────────────────────────────────────
def _tok(sym, chg, mc, fdv, vol):
    return TokenMetrics(sym, sym, 1.0, chg, mc, fdv, vol)


def test_funding_signal_thresholds():
    from wolf.flow.brief import funding_signal
    assert funding_signal(-0.05) == "BULLISH"   # shorts crowded
    assert funding_signal(0.10) == "BEARISH"    # longs overheated
    assert funding_signal(0.0) == "NEUTRAL"
    assert funding_signal(None) is None


# ── report rendering ───────────────────────────────────────────────────────
class StubCG:
    def top_markets(self, limit=60):
        return [_tok("GOOD", -2.5, 50_000_000, 55_000_000, 10_000_000)]
    def global_data(self):
        return GlobalMetrics(53.0, 2.5e12, 1.0)


class StubLlama:
    def chain_activity(self):
        return [ChainActivity("bsc", 1e9, 40.0)]
    def stablecoin_supply(self):
        return StablecoinSupply(1.6e11, 0.2, 0.8)


class StubSentiment:
    def __init__(self, fg=None, cb=None):
        self._fg = fg
        self._cb = cb
    def fear_greed(self):
        return self._fg
    def coinbase_premium(self):
        return self._cb


class StubHL:
    def __init__(self, funding=None, oi=None):
        self._f = funding or {}
        self._oi = oi or {}
    def funding_rate(self, symbol):
        return self._f.get(symbol)
    def open_interest_usd(self, symbol):
        return self._oi.get(symbol)


class StubFunding:
    def get_funding_rate(self, symbol):
        return -0.05 if symbol == "GOODUSDT" else None


# ── Hyperliquid funding/OI ─────────────────────────────────────────────────
def test_hyperliquid_parse_snapshot():
    from wolf.flow.hyperliquid import HyperliquidPerps
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "ENA"}]},
        [{"funding": "0.0000125", "openInterest": "1000", "markPx": "100000"},
         {"funding": "-0.0005", "openInterest": "2000000", "markPx": "0.076"}],
    ]
    snap = HyperliquidPerps.parse(payload)
    assert round(snap["BTC"]["funding_pct"], 5) == 0.00125   # hourly rate × 100
    assert snap["BTC"]["oi_usd"] == 1000 * 100000
    assert round(snap["ENA"]["funding_pct"], 3) == -0.05
    assert HyperliquidPerps.parse({"bad": 1}) == {}


def test_hyperliquid_lookup_by_pair():
    from wolf.flow.hyperliquid import HyperliquidPerps
    hl = HyperliquidPerps()
    hl._cache = {"ENA": {"funding_pct": -0.05, "oi_usd": 152000, "mark_px": 0.076}}
    hl._cache_ts = __import__("time").time()
    assert hl.funding_rate("ENAUSDT") == -0.05
    assert hl.open_interest_usd("ENAUSDT") == 152000


# ── single-token deep dive ─────────────────────────────────────────────────
def test_build_token_view_bull_and_bear():
    from wolf.flow.brief import build_token_view
    # ENA-like: deep from ATH, low FDV/MC, negative funding (bullish) but small.
    t = TokenMetrics("ENA", "Ethena", 0.076, -4.0, 735_000_000, 800_000_000,
                     30_000_000, ath_change_pct=-95.0)
    v = build_token_view(t, funding=-0.05, open_interest_usd=152_000)
    assert v.symbol == "ENA"
    assert any("squeeze" in b for b in v.bull)            # bullish funding
    assert any("dari ATH" in b for b in v.bull)           # flushed downside
    assert v.playbook and "Horizon" in v.playbook[-1]
    assert v.stance in ("ACCUMULATE (conviction)", "NEUTRAL — tunggu konfirmasi", "AVOID — risiko tinggi")


def test_build_token_view_flags_unlock_and_fomo():
    from wolf.flow.brief import build_token_view
    t = TokenMetrics("SPYX", "Spyx", 1.0, 60.0, 20_000_000, 120_000_000,
                     5_000_000, ath_change_pct=-10.0)
    v = build_token_view(t, funding=0.10)   # FDV/MC 6x, pumped +60%, funding bearish
    assert any("unlock" in b for b in v.bear)
    assert any("pump" in b for b in v.bear)
    assert any("overheated" in b for b in v.bear)
    assert "NO leverage — rawan kena likuidasi" in v.playbook


def test_flow_build_token_deep_dive():
    class CG250:
        def top_markets(self, limit=60):
            return [TokenMetrics("ENA", "Ethena", 0.076, -4.0, 735_000_000,
                                 800_000_000, 30_000_000, ath_change_pct=-95.0)]
    rep = TokenDeepDive(coingecko=CG250(),
                        hyperliquid=StubHL(funding={"ENA": -0.05}, oi={"ENA": 152000}),
                        narrator=None, tz="UTC")
    text = rep.build_token("ena")
    assert "DEEP DIVE — $ENA" in text
    assert "Sisi BULLISH" in text and "Sisi BEARISH" in text
    assert "Open interest" in text


def test_flow_build_token_not_found():
    rep = TokenDeepDive(coingecko=StubCG(), hyperliquid=StubHL(), narrator=None, tz="UTC")
    assert rep.build_token("NOPE") is None


# ── OpenAI-compatible client (DeepSeek/Groq) ───────────────────────────────
class _Resp:
    def __init__(self, body):
        self._body = body
    def raise_for_status(self): pass
    def json(self): return self._body


def test_openai_compat_complete(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured.update({"url": url, "headers": headers, "json": json})
        return _Resp({"choices": [{"message": {"content": "hello"}}]})

    monkeypatch.setattr("wolf.ai.openai_compat.requests.post", fake_post)
    c = OpenAICompatLLMClient("k", "https://api.deepseek.com/v1", "deepseek-chat")
    assert c.available
    assert c.complete("sys", "usr") == "hello"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["json"]["model"] == "deepseek-chat"


def test_openai_compat_no_key_unavailable():
    assert not OpenAICompatLLMClient("", "https://x", "m").available


# ── Telegram routing ───────────────────────────────────────────────────────
def test_notify_flow_routes_to_flow_then_news():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1", news_thread_id="55"), session=sess)
    n.notify_flow("hi")
    assert sess.calls[0]["message_thread_id"] == "55"   # falls back to news topic


def test_notify_flow_empty_sends_nothing():
    sess = FakeSession()
    n = TelegramNotifier(TelegramSettings(bot_token="t", chat_id="1"), session=sess)
    n.notify_flow("")
    assert sess.calls == []
