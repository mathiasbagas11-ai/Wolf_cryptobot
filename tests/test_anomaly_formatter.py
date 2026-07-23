"""Phase 5 verification — output formatter + the crucial gate (network-free)."""

from __future__ import annotations

from anomaly.entry import build_ladder
from anomaly.formatter import (
    ENTRY_CONVICTION,
    SHOW_MIN_SCORE,
    format_anomaly_section,
    simplify_verdict,
)


def _coin(symbol, score, *, price=1.50, flagged=False, dca=False, ladder=None):
    return {
        "id": symbol.lower(), "symbol": symbol, "name": f"{symbol} Token",
        "score": score, "flagged": flagged, "in_dca_sleeve": dca,
        "current_price": price,
        "components": {"volatility_contraction": 18, "volume_anomaly": 24,
                       "structure_position": 16, "supply_health": 10},
        "metrics": {"atr_ratio": 0.45, "volume_ratio": 2.4,
                    "range_position": 0.6, "fdv_mc": 1.4},
        "ladder": ladder,
    }


# ── verdict mapping ────────────────────────────────────────────────────────
def test_simplify_verdict_mapping():
    assert simplify_verdict("RISK-ON") == "BULLISH"
    assert simplify_verdict("RISK-ON (contrarian)") == "BULLISH"
    assert simplify_verdict("RISK-OFF") == "BEARISH"
    assert simplify_verdict("ROTATION") == "NEUTRAL"
    assert simplify_verdict("NEUTRAL") == "NEUTRAL"
    assert simplify_verdict(None) == "NEUTRAL"


# ── GATE 1: empty ──────────────────────────────────────────────────────────
def test_gate_no_coin_above_55_shows_empty_message():
    scored = [_coin("AAA", SHOW_MIN_SCORE - 1), _coin("BBB", 40)]
    out = format_anomaly_section(scored, [], market_verdict="BULLISH")
    assert "Tidak ada anomali terdeteksi. Sabar." in out
    assert "ANOMALY PICKS" not in out
    assert "WATCHLIST" not in out


def test_gate_empty_when_scored_list_empty():
    out = format_anomaly_section([], [], market_verdict="BULLISH")
    assert "Tidak ada anomali terdeteksi. Sabar." in out


# ── GATE 2: weak market + low conviction → watchlist, no ladder ────────────
def test_gate_weak_market_low_conviction_is_watchlist_without_ladder():
    lad = {"l1": 1.49, "l2": 1.45, "l3": 1.40, "invalidation": 1.2,
           "tp1": 1.8, "tp2": 2.1, "rr_ratio": 2.0, "sizes": {}}
    scored = [_coin("AAA", 60, ladder=lad)]     # >=55 but <65
    for verdict in ("BEARISH", "NEUTRAL"):
        out = format_anomaly_section(scored, [], market_verdict=verdict)
        assert "👀 <b>WATCHLIST — belum ada entry</b>" in out
        assert "Entry ladder" not in out         # ladder withheld
        assert "$AAA" in out


def test_gate_weak_market_high_conviction_shows_ladder():
    # score >= 65 overrides weak-market caution.
    lad = {"l1": 1.49, "l2": 1.45, "l3": 1.40, "invalidation": 1.2,
           "tp1": 1.8, "tp2": 2.1, "rr_ratio": 2.0,
           "sizes": {"l1": 0.4, "l2": 0.35, "l3": 0.25, "tp1": 0.3,
                     "tp2": 0.3, "runner": 0.4, "trailing_stop_pct": 0.2}}
    scored = [_coin("AAA", ENTRY_CONVICTION, ladder=lad)]
    out = format_anomaly_section(scored, [], market_verdict="BEARISH")
    assert "ANOMALY PICKS" in out
    assert "Entry ladder" in out


def test_gate_bullish_market_shows_ladder_even_below_65():
    lad = {"l1": 1.49, "l2": 1.45, "l3": 1.40, "invalidation": 1.2,
           "tp1": 1.8, "tp2": 2.1, "rr_ratio": 2.0,
           "sizes": {"l1": 0.4, "l2": 0.35, "l3": 0.25, "tp1": 0.3,
                     "tp2": 0.3, "runner": 0.4, "trailing_stop_pct": 0.2}}
    scored = [_coin("AAA", 58, ladder=lad)]     # <65 but market bullish
    out = format_anomaly_section(scored, [], market_verdict="BULLISH")
    assert "ANOMALY PICKS" in out
    assert "Entry ladder" in out


# ── rendering details ──────────────────────────────────────────────────────
def test_display_capped_and_sorted_by_score():
    scored = [_coin(f"C{i}", 60 + i) for i in range(6)]
    out = format_anomaly_section(scored, [], market_verdict="BULLISH", max_display=3)
    # top 3 by score are C5(65), C4(64), C3(63); C0/C1/C2 excluded
    assert "$C5" in out and "$C4" in out and "$C3" in out
    assert "$C2" not in out and "$C0" not in out


def test_flagged_rendered_in_footer_and_paper_mode_note():
    scored = [_coin("AAA", 70, ladder={"l1": 1.49, "l2": 1.4, "l3": 1.3,
              "invalidation": 1.1, "tp1": 1.8, "tp2": 2.1, "rr_ratio": 2.0, "sizes": {}})]
    flagged = [{"symbol": "BAD", "flags": ["wash_volume(turnover 4.0x)"]}]
    out = format_anomaly_section(scored, flagged, market_verdict="BULLISH")
    assert "FLAGGED" in out and "$BAD" in out
    assert "PAPER MODE" in out


def test_dca_sleeve_tagged():
    scored = [_coin("SOL", 70, dca=True, ladder={"l1": 1.49, "l2": 1.4, "l3": 1.3,
              "invalidation": 1.1, "tp1": 1.8, "tp2": 2.1, "rr_ratio": 2.0, "sizes": {}})]
    out = format_anomaly_section(scored, [], market_verdict="BULLISH")
    assert "DCA sleeve" in out


def test_flagged_coins_never_appear_as_picks():
    scored = [_coin("AAA", 90, flagged=True)]    # flagged leaked into scored
    out = format_anomaly_section(scored, [], market_verdict="BULLISH")
    assert "Tidak ada anomali terdeteksi. Sabar." in out
