"""Tests for the flow-intelligence layer: parsing, brief logic, report, narrator."""

from __future__ import annotations

from wolf.ai.base import LLMClient
from wolf.ai.openai_compat import OpenAICompatLLMClient
from wolf.config import TelegramSettings
from wolf.flow import build_brief
from wolf.flow.coingecko import CoinGeckoClient, GlobalMetrics, TokenMetrics
from wolf.flow.defillama import ChainActivity, DefiLlamaClient, StablecoinSupply
from wolf.notify import TelegramNotifier
from wolf.reports import FlowReporter

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


def test_brief_picks_and_skips():
    markets = [
        _tok("GOOD", 5.0, 50_000_000, 55_000_000, 10_000_000),   # FDV/MC 1.1, vol 20% → PICK
        _tok("UNLOCK", 3.0, 10_000_000, 60_000_000, 2_000_000),  # FDV/MC 6x → SKIP
        _tok("PUMPED", 80.0, 20_000_000, 21_000_000, 5_000_000), # +80% → SKIP FOMO
        _tok("WASH", 1.0, 10_000_000, 11_000_000, 50_000_000),   # vol 5x mcap → SKIP wash
        _tok("USDT", 0.0, 1e11, 1e11, 9e10),                     # stablecoin → excluded
    ]
    brief = build_brief(markets, None, [], None)
    assert [p.symbol for p in brief.picks] == ["GOOD"]
    skip_syms = {s.symbol for s in brief.skips}
    assert {"UNLOCK", "PUMPED", "WASH"} <= skip_syms
    assert "USDT" not in skip_syms


def test_brief_liquidity_percentile_and_watchlist():
    # 5 tradable tokens with increasing turnover → percentile ranks them.
    markets = [
        _tok("A", 1.0, 50_000_000, 55_000_000, 6_000_000),    # vol_mc 0.12
        _tok("B", 1.0, 50_000_000, 55_000_000, 10_000_000),   # 0.20
        _tok("C", 1.0, 50_000_000, 55_000_000, 20_000_000),   # 0.40
        _tok("D", 1.0, 50_000_000, 55_000_000, 30_000_000),   # 0.60
    ]
    brief = build_brief(markets, None, [], None, max_picks=2, max_watch=2)
    assert len(brief.picks) == 2 and len(brief.watchlist) == 2
    # Highest turnover token ranks at the top percentile.
    top = max(brief.picks + brief.watchlist, key=lambda p: p.liquidity_pctile)
    assert top.symbol == "D" and top.liquidity_pctile == 100.0


def test_funding_signal_thresholds():
    from wolf.flow.brief import funding_signal
    assert funding_signal(-0.05) == "BULLISH"   # shorts crowded
    assert funding_signal(0.10) == "BEARISH"    # longs overheated
    assert funding_signal(0.0) == "NEUTRAL"
    assert funding_signal(None) is None


def test_pick_quant_score_rewards_funding_and_low_fdv():
    from wolf.flow.brief import Pick
    p = Pick("X", "X", 1, 2.0, 50e6, fdv_mc=1.0, vol_mc=0.5,
             liquidity_pctile=90.0, funding_rate=-0.05)
    assert p.funding_signal == "BULLISH"
    assert p.quant_score >= 80   # low unlock + high liquidity + bullish funding


def test_brief_stance_risk_on():
    g = GlobalMetrics(btc_dominance=52.0, total_market_cap=2.5e12, market_cap_change_24h=2.0)
    s = StablecoinSupply(total_usd=1.6e11, change_1d_pct=0.3, change_7d_pct=1.2)
    chains = [ChainActivity("bsc", 1e9, 50.0)]
    brief = build_brief([], g, chains, s)
    assert brief.stance == "RISK-ON"
    assert "BNB" in brief.conclusion


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


class StubFunding:
    def get_funding_rate(self, symbol):
        return -0.05 if symbol == "GOODUSDT" else None


def test_flow_report_template_fallback():
    rep = FlowReporter(coingecko=StubCG(), defillama=StubLlama(), narrator=None, tz="UTC")
    text = rep.build()
    assert "FLOW INTELLIGENCE" in text
    assert "$GOOD" in text and "BNB" in text
    assert "Quant" in text and "KESIMPULAN" in text


def test_flow_report_enriches_funding_from_market_client():
    rep = FlowReporter(coingecko=StubCG(), defillama=StubLlama(), narrator=None,
                       market_client=StubFunding(), tz="UTC")
    brief = rep.gather()
    assert brief.picks[0].funding_rate == -0.05
    assert brief.picks[0].funding_signal == "BULLISH"
    text = rep.build()
    assert "Funding BULLISH" in text


class FakeNarrator(LLMClient):
    def __init__(self):
        self.prompt = ""
    @property
    def available(self):
        return True
    def complete(self, system, user, *, max_tokens=1024):
        self.prompt = user
        return "1/ BTC & MARKET\n🟢 risk-on bro"
    def complete_json(self, system, user, schema, *, max_tokens=1024):
        return {}


def test_flow_report_uses_narrator_and_passes_numbers():
    narr = FakeNarrator()
    rep = FlowReporter(coingecko=StubCG(), defillama=StubLlama(), narrator=narr, tz="UTC")
    text = rep.build()
    assert "risk-on bro" in text
    # narrator received the real numbers, not invented ones
    assert "GOOD" in narr.prompt and "token_picks" in narr.prompt


def test_flow_report_empty_returns_none():
    class Empty:
        def top_markets(self, limit=60): return []
        def global_data(self): return None
    class EmptyL:
        def chain_activity(self): return []
        def stablecoin_supply(self): return None
    rep = FlowReporter(coingecko=Empty(), defillama=EmptyL(), narrator=None)
    assert rep.build() is None


# ── OpenAI-compatible client (DeepSeek/Groq) ───────────────────────────────
class _Resp:
    def __init__(self, body):
        self._body = body
    def raise_for_status(self): pass
    def json(self): return self._body


class _Sess:
    def __init__(self, body):
        self._body = body
        self.posted = None
    def post(self, url, headers=None, json=None, timeout=None):
        self.posted = {"url": url, "headers": headers, "json": json}
        return _Resp(self._body)


def test_openai_compat_complete():
    sess = _Sess({"choices": [{"message": {"content": "hello"}}]})
    c = OpenAICompatLLMClient("k", "https://api.deepseek.com/v1", "deepseek-chat", session=sess)
    assert c.available
    assert c.complete("sys", "usr") == "hello"
    assert sess.posted["headers"]["Authorization"] == "Bearer k"
    assert sess.posted["json"]["model"] == "deepseek-chat"


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
