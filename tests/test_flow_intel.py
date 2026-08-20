"""Tests for the Flow Intelligence digest.

The report is a pure function of the StateStore, so every case below seeds a
store and reads the rendered string. Several tests exist specifically to pin the
four rules the previous report broke: no entry calls, no pegged assets in the
watchlist, no untradeable symbols, and labels that match their numbers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from wolf.reports.flow import (
    FlowIntelReporter,
    build_watchlist,
    decide_verdict,
    flow_change_label,
    is_pegged,
    stale_note,
)


def _ts(minutes_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _macro(ts_minutes: float = 1.0, **overrides) -> dict:
    doc = {
        "ts": _ts(ts_minutes),
        "global": {"btc_dominance": 54.2, "usdt_dominance": 4.8,
                   "total_market_cap": 3.1e12, "market_cap_change_24h": 1.8},
        "stablecoin": {"total_usd": 180e9, "change_1d_pct": 0.2, "change_7d_pct": 1.4},
        "chains": [
            {"chain": "solana", "label": "Solana", "dex_volume_24h": 4.2e9, "change_1d": 12.0},
            {"chain": "base", "label": "Base", "dex_volume_24h": 1.1e9, "change_1d": -3.0},
        ],
        "markets": [
            {"symbol": "SOL", "name": "Solana", "price": 200.0, "change_24h": 3.2,
             "market_cap": 90e9, "fdv": 110e9, "volume_24h": 9e9, "ath_change_pct": -25.0},
        ],
    }
    doc.update(overrides)
    return doc


def _whale(ts_minutes: float = 1.0) -> dict:
    return {
        "ts": _ts(ts_minutes),
        # ``coins`` = who moved in the last scan window (an event).
        # ``bias``  = where every tracked wallet is sitting (the positioning).
        "coins": {"SOL": {"direction": "LONG", "wallet_count": 4, "notional_usd": 2_400_000}},
        "bias": {"SOL": {"long_count": 4, "short_count": 0,
                         "long_notional": 2_400_000.0, "short_notional": 0.0}},
    }


def _premium(ts_minutes: float = 1.0, pct: float = 0.12, signal: str = "ACCUMULATION") -> dict:
    return {"ts": _ts(ts_minutes), "premium_pct": pct, "signal": signal,
            "available": True, "symbol": "BTC"}


class _StubUniverse:
    def __init__(self, symbols=("SOLUSDT", "ETHUSDT", "AVAXUSDT")) -> None:
        self._symbols = list(symbols)

    def symbols(self):
        return list(self._symbols)


def _seed(store, *, macro=None, whale=None, premium=None) -> None:
    store.write("flow_macro", macro if macro is not None else _macro())
    store.write("whale_hyperliquid", whale if whale is not None else _whale())
    store.write("coinbase_premium", premium if premium is not None else _premium())


def _report(store, **kw) -> str:
    return FlowIntelReporter(store, _StubUniverse(), tz="UTC", **kw).build()


def _plain(store, **kw) -> str:
    """Report with the Telegram markup stripped, so assertions read like the message."""
    return re.sub(r"</?[bi]>", "", _report(store, **kw))


# ── the hard rule: no entry calls anywhere in the output ──────────────────
def test_report_never_mentions_an_entry_price():
    """The old report printed 'entry zone: sekarang' derived from the 24h change."""
    import wolf.reports.flow as flow_mod

    class _Store:
        def read(self, key, default=None):
            return {"flow_macro": _macro(), "whale_hyperliquid": _whale(),
                    "coinbase_premium": _premium()}.get(key, default)

    text = flow_mod.FlowIntelReporter(_Store(), _StubUniverse(), tz="UTC").build().lower()
    for banned in ("entry", "masuk di", "beli di", "buy at", "target", "stop loss", " sl ", " tp "):
        assert banned not in text, f"flow report must not contain {banned!r}"


def test_report_states_its_boundary_explicitly(store):
    _seed(store)
    assert "bukan harga eksekusi" in _report(store)


# ── section structure ─────────────────────────────────────────────────────
def test_report_renders_all_six_sections(store):
    _seed(store)
    text = _report(store)
    for heading in ("1/ MARKET MACRO", "2/ DRY POWDER", "3/ CHAIN ROTATION",
                    "4/ INSTITUTIONAL FLOW", "5/ WHALE POSITIONING", "6/ WATCHLIST"):
        assert heading in text


def test_report_renders_the_numbers_it_was_given(store):
    _seed(store)
    text = _report(store)
    assert "+1.8%" in text                  # market cap change
    assert "54.2%" in text                  # BTC dominance
    assert "+0.120%" in text                # Coinbase premium
    assert "4 wallet" in text               # whale coordination


def test_report_returns_none_without_any_collector_data(store):
    assert FlowIntelReporter(store, _StubUniverse()).build() is None


def test_missing_sections_degrade_individually(store):
    store.write("flow_macro", {"ts": _ts(), "global": None, "stablecoin": None,
                               "chains": [], "markets": []})
    store.write("whale_hyperliquid", _whale())
    text = _report(store)
    assert "Data macro belum tersedia" in text
    assert "WHALE POSITIONING" in text and "$SOL" in text


def test_no_whale_data_at_all_says_so_plainly(store):
    _seed(store, whale={"ts": _ts(), "coins": {}, "bias": {}})
    assert "Belum ada posisi whale terlacak" in _report(store)


def test_quiet_window_still_reports_standing_positions(store):
    """The bug: ten minutes after a coordinated entry ``coins`` empties, and the
    section announced "no coordination" while six whales sat long on the coin."""
    _seed(store, whale={
        "ts": _ts(),
        "coins": {},                       # nobody moved this window
        "bias": {"SOL": {"long_count": 6, "short_count": 0,
                         "long_notional": 3_000_000.0, "short_notional": 0.0}},
    })
    text = _report(store)

    assert "$SOL LONG — 6L / 0S" in _plain(store)
    assert "Belum ada posisi whale" not in text
    assert "Baru bergerak" not in text, "no entries this window, so no event line"


def test_section_five_separates_positioning_from_this_window(store):
    _seed(store)
    text = _report(store)

    assert "$SOL LONG — 4L / 0S" in _plain(store)          # positioning
    assert "Baru bergerak window ini: $SOL LONG (4 wallet)" in text   # event


def test_positioning_shows_both_sides_of_a_split_book(store):
    _seed(store, whale={
        "ts": _ts(), "coins": {},
        "bias": {"SOL": {"long_count": 5, "short_count": 2,
                         "long_notional": 5_000_000.0, "short_notional": 900_000.0}},
    })
    assert "$SOL LONG — 5L / 2S" in _plain(store)


def test_balanced_book_is_not_listed_as_a_direction(store):
    _seed(store, whale={
        "ts": _ts(), "coins": {},
        "bias": {"SOL": {"long_count": 3, "short_count": 3,
                         "long_notional": 1e6, "short_notional": 1e6}},
    })
    assert "$SOL" not in _plain(store).split("6/ WATCHLIST")[0].split("5/ WHALE")[1]


# ── staleness markers ─────────────────────────────────────────────────────
def test_stale_whale_data_is_shown_with_its_age(store):
    _seed(store, whale=_whale(ts_minutes=45.0))
    text = _report(store)
    assert "data 45m lalu" in text
    assert "$SOL" in text, "stale data is still shown, just labelled"


def test_fresh_data_carries_no_age_marker(store):
    _seed(store)
    assert "lalu)" not in _report(store)


def test_stale_note_formats():
    assert stale_note(_ts(2.0)) == ""
    assert stale_note(_ts(45.0)) == " (data 45m lalu)"
    assert "j lalu" in stale_note(_ts(200.0))
    assert stale_note(None) == " (umur data tidak diketahui)"


# ── labels must match the numbers ─────────────────────────────────────────
def test_zero_change_is_not_labelled_as_inflow():
    """The old report called a 0.0% change 'numpuk 🔥'."""
    assert "numpuk" not in flow_change_label(0.0)
    assert "flat" in flow_change_label(0.0)
    assert "flat" in flow_change_label(0.01)


def test_real_changes_get_directional_labels():
    assert "numpuk" in flow_change_label(1.4)
    assert "nyusut" in flow_change_label(-1.4)


def test_flat_stablecoin_supply_renders_flat(store):
    macro = _macro()
    macro["stablecoin"]["change_7d_pct"] = 0.0
    _seed(store, macro=macro)
    text = _report(store)
    assert "numpuk" not in text
    assert "flat" in text


def test_flat_chain_volume_gets_a_neutral_marker(store):
    macro = _macro()
    macro["chains"] = [{"chain": "base", "label": "Base", "dex_volume_24h": 1e9, "change_1d": 0.0}]
    _seed(store, macro=macro)
    assert "⚪ Base" in _report(store)


# ── watchlist filters ─────────────────────────────────────────────────────
def test_is_pegged_by_ticker():
    assert is_pegged("USDT")
    assert is_pegged("CRVUSD")
    assert is_pegged("WBTC")
    assert not is_pegged("SOL")


def test_is_pegged_by_name_for_tickers_no_list_can_anticipate():
    assert is_pegged("FIDD", "Fidelity USD Fund")
    assert is_pegged("SNDKB", "SanDisk xStock Tokenized Equity")
    assert is_pegged("STETH2", "Staked Ether v2")
    assert not is_pegged("AVAX", "Avalanche")


def test_watchlist_excludes_stablecoins_and_tokenized_stocks():
    """$CRVUSD and $SNDKB screened well on FDV/MC 1.0x — trivially true when pegged."""
    markets = [
        {"symbol": "CRVUSD", "name": "Curve.fi USD", "market_cap": 1e9, "fdv": 1e9, "volume_24h": 5e8},
        {"symbol": "SNDKB", "name": "SanDisk xStock", "market_cap": 1e9, "fdv": 1e9, "volume_24h": 5e8},
        {"symbol": "SOL", "name": "Solana", "market_cap": 90e9, "fdv": 110e9, "volume_24h": 9e9},
    ]
    assert [r["symbol"] for r in build_watchlist(markets)] == ["SOL"]


def test_watchlist_excludes_symbols_absent_from_the_exchange_universe():
    """A hit whose get_klines() returns [] is not a finding."""
    markets = [
        {"symbol": "OBSCURE", "name": "Obscure Chain", "market_cap": 5e8, "fdv": 5e8, "volume_24h": 4e8},
        {"symbol": "SOL", "name": "Solana", "market_cap": 90e9, "fdv": 110e9, "volume_24h": 9e9},
    ]
    picks = build_watchlist(markets, tradeable_bases={"SOL", "ETH"})
    assert [r["symbol"] for r in picks] == ["SOL"]


def test_watchlist_filter_is_disabled_rather_than_emptied_without_a_universe():
    markets = [{"symbol": "OBSCURE", "name": "Obscure", "market_cap": 5e8,
                "fdv": 5e8, "volume_24h": 4e8}]
    assert build_watchlist(markets, None), "no universe wired → do not filter everything out"


def test_watchlist_excludes_majors_and_dust():
    markets = [
        {"symbol": "BTC", "name": "Bitcoin", "market_cap": 2e12, "fdv": 2e12, "volume_24h": 5e10},
        {"symbol": "DUST", "name": "Dust", "market_cap": 1e6, "fdv": 1e6, "volume_24h": 5e5},
        {"symbol": "SOL", "name": "Solana", "market_cap": 90e9, "fdv": 110e9, "volume_24h": 9e9},
    ]
    assert [r["symbol"] for r in build_watchlist(markets)] == ["SOL"]


def test_watchlist_ranks_by_turnover_and_respects_the_limit():
    markets = [
        {"symbol": "LOW", "name": "Low", "market_cap": 1e9, "fdv": 1e9, "volume_24h": 1e7},
        {"symbol": "HIGH", "name": "High", "market_cap": 1e9, "fdv": 1e9, "volume_24h": 9e8},
        {"symbol": "MID", "name": "Mid", "market_cap": 1e9, "fdv": 1e9, "volume_24h": 2e8},
    ]
    assert [r["symbol"] for r in build_watchlist(markets)] == ["HIGH", "MID", "LOW"]
    assert len(build_watchlist(markets, limit=2)) == 2


def test_watchlist_carries_no_price_recommendation_fields():
    markets = [{"symbol": "SOL", "name": "Solana", "market_cap": 90e9,
                "fdv": 110e9, "volume_24h": 9e9}]
    row = build_watchlist(markets)[0]
    assert not {"entry", "entry_note", "tp", "sl", "target"} & set(row)


def test_watchlist_section_reports_nothing_when_all_candidates_are_filtered(store):
    macro = _macro()
    macro["markets"] = [{"symbol": "USDC", "name": "USD Coin", "market_cap": 5e10,
                         "fdv": 5e10, "volume_24h": 1e10}]
    _seed(store, macro=macro)
    assert "Belum ada kandidat yang lolos filter" in _report(store)


def test_universe_failure_disables_the_filter_without_crashing(store):
    class _BrokenUniverse:
        def symbols(self):
            raise ValueError("upstream down")

    _seed(store)
    text = FlowIntelReporter(store, _BrokenUniverse(), tz="UTC").build()
    assert "$SOL" in text


# ── verdict ───────────────────────────────────────────────────────────────
def test_verdict_neutral_without_evidence():
    assert decide_verdict(None, None, None, {}) == "NEUTRAL"


def test_verdict_risk_on_when_evidence_agrees():
    verdict = decide_verdict(
        {"market_cap_change_24h": 2.5},
        {"change_7d_pct": 1.4},
        {"signal": "ACCUMULATION"},
        {"SOL": {"direction": "LONG"}},
    )
    assert verdict == "RISK-ON"


def test_verdict_risk_off_when_evidence_agrees():
    verdict = decide_verdict(
        {"market_cap_change_24h": -3.0},
        {"change_7d_pct": -1.2},
        {"signal": "DISTRIBUTION"},
        {"SOL": {"direction": "SHORT"}},
    )
    assert verdict == "RISK-OFF"


def test_verdict_rotation_when_evidence_splits_evenly():
    verdict = decide_verdict(
        {"market_cap_change_24h": 2.0},
        {"change_7d_pct": -1.2},
        None, {},
    )
    assert verdict == "ROTATION"


def test_neutral_verdict_carries_no_execution_advice(store):
    """A NEUTRAL read must not sit under a call to act."""
    store.write("flow_macro", {
        "ts": _ts(),
        "global": {"btc_dominance": 54.0, "usdt_dominance": 4.8,
                   "total_market_cap": 3.1e12, "market_cap_change_24h": 0.1},
        "stablecoin": {"total_usd": 180e9, "change_1d_pct": 0.0, "change_7d_pct": 0.0},
        "chains": [], "markets": [],
    })
    store.write("whale_hyperliquid", {"ts": _ts(), "coins": {}})
    store.write("coinbase_premium", {"ts": _ts(), "available": False, "signal": "NEUTRAL",
                                     "premium_pct": None})

    text = _report(store)
    assert "KESIMPULAN: NEUTRAL" in text
    assert "Tidak ada yang perlu dikejar" in text
    for banned in ("mendukung setup LONG", "jalan lebih berat"):
        assert banned not in text


def test_verdict_matches_the_body_it_sits_under(store):
    _seed(store)   # bullish macro + accumulation + whales long
    text = _report(store)
    assert "KESIMPULAN: RISK-ON" in text


# ── output safety ─────────────────────────────────────────────────────────
def test_report_escapes_untrusted_token_names(store):
    macro = _macro()
    macro["markets"] = [{"symbol": "EVIL", "name": "<script>x</script>", "market_cap": 1e9,
                         "fdv": 1e9, "volume_24h": 5e8}]
    _seed(store, macro=macro)
    text = FlowIntelReporter(store, _StubUniverse(("EVILUSDT",)), tz="UTC").build()
    assert "<script>" not in text


def test_report_has_no_unclosed_bold_tags(store):
    _seed(store)
    text = _report(store)
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")


# ── anomaly section ───────────────────────────────────────────────────────
def test_anomaly_section_absent_when_not_wired(store):
    _seed(store)
    assert FlowIntelReporter(store, _StubUniverse())._anomaly_section("NEUTRAL") == ""


def test_anomaly_section_appended_when_wired(store):
    class _Fake:
        def build_section(self, verdict):
            return f"ANOMALY::{verdict}"

    _seed(store)
    text = _report(store, anomaly=_Fake())
    assert "ANOMALY::" in text


def test_anomaly_failure_does_not_cost_the_digest(store):
    class _Boom:
        def build_section(self, verdict):
            raise RuntimeError("scanner exploded")

    _seed(store)
    text = _report(store, anomaly=_Boom())
    assert "Anomaly scan gagal" in text
    assert "1/ MARKET MACRO" in text, "the rest of the report still goes out"


def test_trillion_market_caps_render_as_trillions(store):
    """The shared fmt_usd tops out at 'B', which reads badly for a $3T market."""
    from wolf.reports.flow import fmt_big_usd

    assert fmt_big_usd(3.1e12) == "$3.10T"
    assert fmt_big_usd(180e9) == "$180.00B"

    _seed(store)
    assert "$3.10T" in _report(store)
