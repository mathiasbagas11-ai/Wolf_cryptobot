"""Phase 7 verification — scan orchestrator (fully injected, network-free)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from anomaly.scanner import AnomalyScanner

TS = pd.date_range("2026-01-01", periods=90, freq="D", tz="UTC")


def _df():
    close = 100 + np.cumsum(np.full(90, 0.05))
    return pd.DataFrame({"timestamp": TS, "open": close, "high": close * 1.02,
                         "low": close * 0.98, "close": close,
                         "volume": np.full(90, 1_000_000.0)})


def _universe(n=5):
    return [{"id": f"coin{i}", "symbol": f"C{i}", "name": f"Coin {i}",
             "current_price": 1.5, "market_cap": 2e8, "total_volume": 2e7,
             "fdv": 3e8, "price_change_percentage_24h": 1.0, "in_dca_sleeve": False}
            for i in range(n)]


def _fake_score(scores):
    """Return a score_fn yielding preset scores/flags per coin_id."""
    def score_fn(df, coin):
        spec = scores.get(coin["id"], {"score": 60})
        return {"id": coin["id"], "symbol": coin["symbol"], "name": coin["name"],
                "score": spec["score"], "flagged": spec.get("flagged", False),
                "flags": spec.get("flags", []), "in_dca_sleeve": False,
                "components": {"volatility_contraction": 15, "volume_anomaly": 20,
                               "structure_position": 15, "supply_health": 10},
                "metrics": {"fdv_mc": 1.4, "turnover": 0.1, "volume_ratio": 2.0,
                            "range_position": 0.6}}
    return score_fn


def _scanner(universe, scores, **over):
    kw = dict(
        universe_fn=lambda: universe,
        ohlc_fn=lambda cid, **k: _df(),
        score_fn=_fake_score(scores),
        ladder_fn=lambda df, price: {"l1": price * 0.99, "l2": price * 0.97,
                                     "l3": price * 0.95, "invalidation": price * 0.9,
                                     "tp1": price * 1.1, "tp2": price * 1.2,
                                     "rr_ratio": 2.0, "atr14": 0.05, "sizes": {}},
    )
    kw.update(over)
    return AnomalyScanner(**kw)


# ── pipeline routing ───────────────────────────────────────────────────────
def test_scan_routes_scored_and_flagged_and_enriches():
    scores = {"coin0": {"score": 80}, "coin1": {"score": 40},
              "coin2": {"score": 70, "flagged": True, "flags": ["wash_volume"]}}
    result = _scanner(_universe(3), scores).scan("BULLISH")
    assert result["universe"] == 3 and result["scanned"] == 3
    scored_ids = {c["id"] for c in result["scored"]}
    flagged_ids = {c["id"] for c in result["flagged"]}
    assert scored_ids == {"coin0", "coin1"} and flagged_ids == {"coin2"}
    top = result["scored"][0]                        # sorted by score desc
    assert top["id"] == "coin0"
    assert top["current_price"] == 1.5 and top["market_cap"] == 2e8
    assert "ladder" in top                           # non-flagged gets a ladder


def test_flagged_coin_gets_no_ladder():
    scores = {"coin0": {"score": 70, "flagged": True, "flags": ["unlock_overhang"]}}
    result = _scanner(_universe(1), scores).scan("BULLISH")
    assert "ladder" not in result["flagged"][0]


def test_scan_limit_caps_coins_scanned():
    result = _scanner(_universe(10), {}, scan_limit=4).scan("BULLISH")
    assert result["scanned"] == 4


def test_time_budget_stops_early():
    # A clock that jumps past the budget after the 2nd coin.
    ticks = iter([0, 1, 2, 999, 999, 999, 999])
    scanner = _scanner(_universe(5), {}, clock=lambda: next(ticks), time_budget_sec=100)
    result = scanner.scan("BULLISH")
    assert result["scanned"] < 5


def test_one_bad_coin_does_not_abort_scan():
    def flaky_ohlc(cid, **k):
        if cid == "coin1":
            raise RuntimeError("boom")
        return _df()
    result = _scanner(_universe(3), {}, ohlc_fn=flaky_ohlc).scan("BULLISH")
    assert result["scanned"] == 2                    # coin1 skipped, others fine


def test_empty_universe_yields_empty_result():
    result = _scanner([], {}).scan("BULLISH")
    assert result == {"scored": [], "flagged": [], "scanned": 0, "universe": 0}


# ── section + logging ──────────────────────────────────────────────────────
def test_build_section_renders_and_logs():
    logged = {}

    class FakeLogger:
        def log_signals(self, scored, verdict):
            logged["count"] = len(scored)
            logged["verdict"] = verdict
            return len(scored)

    scores = {"coin0": {"score": 80}, "coin1": {"score": 70}}
    scanner = _scanner(_universe(2), scores, logger=FakeLogger())
    section = scanner.build_section("BULLISH")
    assert "ANOMALY SCANNER" in section
    assert "ANOMALY PICKS" in section                # bullish + high score → picks
    assert logged["count"] == 2 and logged["verdict"] == "BULLISH"


def test_build_section_gate_empty_when_all_below_min():
    scores = {f"coin{i}": {"score": 40} for i in range(3)}
    section = _scanner(_universe(3), scores).build_section("BULLISH")
    assert "Tidak ada anomali terdeteksi. Sabar." in section


def test_logging_failure_never_breaks_section():
    class BoomLogger:
        def log_signals(self, scored, verdict):
            raise RuntimeError("sheets down")

    scores = {"coin0": {"score": 80}}
    section = _scanner(_universe(1), scores, logger=BoomLogger()).build_section("BULLISH")
    assert "ANOMALY SCANNER" in section              # section still rendered


# ── backfill wiring ────────────────────────────────────────────────────────
def test_run_backfill_fetches_prices_for_open_ids():
    calls = {}

    class FakeLogger:
        def open_coin_ids(self):
            return ["coin0", "coin1"]
        def backfill_outcomes(self, price_lookup):
            calls["lookup0"] = price_lookup("coin0")
            calls["lookup_missing"] = price_lookup("nope")
            return {"scanned": 2, "updated": 1, "closed": 0}

    scanner = _scanner(_universe(1), {}, logger=FakeLogger(),
                       prices_fn=lambda ids: {"coin0": 2.0, "coin1": 3.0})
    summary = scanner.run_backfill()
    assert summary == {"scanned": 2, "updated": 1, "closed": 0}
    assert calls["lookup0"] == 2.0 and calls["lookup_missing"] is None


def test_run_backfill_no_logger_is_noop():
    assert _scanner(_universe(1), {}).run_backfill() == {"scanned": 0, "updated": 0, "closed": 0}
