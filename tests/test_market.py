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
