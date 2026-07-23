"""Phase 6 verification — paper trade logger (in-memory fake sheet, no gspread)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from anomaly.logger import (
    HEADER,
    AnomalyPaperLogger,
    build_row,
)

NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)


class FakeWorksheet:
    """Minimal in-memory stand-in for a gspread worksheet."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_row(self, values):
        self.rows.append(list(values))

    def update_cell(self, row, col, value):     # 1-indexed
        self.rows[row - 1][col - 1] = value


def _coin(symbol="BANK", score=72, **over):
    coin = {
        "id": symbol.lower(), "symbol": symbol, "name": f"{symbol} Token",
        "score": score, "flagged": False, "in_dca_sleeve": False,
        "current_price": 1.50, "market_cap": 2e8,
        "components": {"volatility_contraction": 21, "volume_anomaly": 27,
                       "structure_position": 16, "supply_health": 8},
        "metrics": {"fdv_mc": 1.3, "turnover": 0.1, "volume_ratio": 2.9,
                    "range_position": 0.62},
        "ladder": {"l1": 1.49, "l2": 1.45, "l3": 1.40, "invalidation": 1.2,
                   "tp1": 1.8, "tp2": 2.1, "rr_ratio": 2.0, "atr14": 0.06},
    }
    coin.update(over)
    return coin


# ── row building ───────────────────────────────────────────────────────────
def test_build_row_matches_header_length_and_order():
    row = build_row(_coin(), "BULLISH", NOW)
    assert len(row) == len(HEADER)
    d = dict(zip(HEADER, row))
    assert d["symbol"] == "BANK"
    assert d["score"] == 72
    assert d["score_A"] == 21 and d["score_D"] == 8
    assert d["price_at_signal"] == 1.5
    assert d["l1"] == 1.49 and d["tp2"] == 2.1
    assert d["market_verdict"] == "BULLISH"
    assert d["in_dca_sleeve"] == "FALSE"
    assert d["status"] == "OPEN"
    assert d["outcome_7d"] == "" and d["outcome_30d"] == "" and d["notes"] == ""
    # atr_ratio = atr14 / price
    assert d["atr_ratio"] == round(0.06 / 1.5, 4)


def test_build_row_blanks_missing_ladder():
    row = build_row(_coin(ladder=None), "NEUTRAL", NOW)
    d = dict(zip(HEADER, row))
    assert d["l1"] == "" and d["invalidation"] == "" and d["atr_ratio"] == ""


# ── logging ────────────────────────────────────────────────────────────────
def test_log_signals_writes_header_then_qualifying_rows():
    ws = FakeWorksheet()
    logger = AnomalyPaperLogger(ws)
    coins = [_coin("AAA", 72), _coin("BBB", 54),          # 54 < 55 → skipped
             _coin("CCC", 90, flagged=True)]              # flagged → skipped
    n = logger.log_signals(coins, "BULLISH", now=NOW)
    assert n == 1
    assert ws.rows[0] == HEADER
    assert ws.rows[1][HEADER.index("symbol")] == "AAA"
    assert len(ws.rows) == 2


def test_log_signals_reuses_existing_header():
    ws = FakeWorksheet([HEADER])
    logger = AnomalyPaperLogger(ws)
    logger.log_signals([_coin("AAA", 60)], "BULLISH", now=NOW)
    assert ws.rows[0] == HEADER
    assert len([r for r in ws.rows if r == HEADER]) == 1     # header not duplicated


# ── backfill ───────────────────────────────────────────────────────────────
def _sheet_with_signal(age_days, price0=1.50):
    ws = FakeWorksheet([HEADER])
    ts = (NOW - timedelta(days=age_days)).isoformat()
    row = build_row(_coin(), "BULLISH", NOW - timedelta(days=age_days))
    row[HEADER.index("timestamp")] = ts
    row[HEADER.index("price_at_signal")] = price0
    ws.append_row(row)
    return ws


def test_backfill_fills_7d_when_elapsed_and_marks_pct():
    ws = _sheet_with_signal(age_days=8, price0=1.50)
    logger = AnomalyPaperLogger(ws)
    summary = logger.backfill_outcomes(lambda cid: 1.80, now=NOW)   # +20%
    d = dict(zip(HEADER, ws.rows[1]))
    assert d["outcome_7d"] == 20.0
    assert d["outcome_14d"] == "" and d["outcome_30d"] == ""
    assert d["status"] == "OPEN"
    assert summary == {"scanned": 1, "updated": 1, "closed": 0}


def test_backfill_does_not_fill_before_window():
    ws = _sheet_with_signal(age_days=3)
    logger = AnomalyPaperLogger(ws)
    logger.backfill_outcomes(lambda cid: 1.65, now=NOW)
    d = dict(zip(HEADER, ws.rows[1]))
    assert d["outcome_7d"] == "" and d["status"] == "OPEN"


def test_backfill_closes_after_30_days_and_fills_all():
    ws = _sheet_with_signal(age_days=31, price0=2.00)
    logger = AnomalyPaperLogger(ws)
    summary = logger.backfill_outcomes(lambda cid: 1.00, now=NOW)   # -50%
    d = dict(zip(HEADER, ws.rows[1]))
    assert d["outcome_7d"] == -50.0 and d["outcome_14d"] == -50.0 and d["outcome_30d"] == -50.0
    assert d["status"] == "CLOSED"
    assert summary["closed"] == 1


def test_backfill_skips_closed_rows():
    ws = _sheet_with_signal(age_days=40)
    ws.rows[1][HEADER.index("status")] = "CLOSED"
    logger = AnomalyPaperLogger(ws)
    summary = logger.backfill_outcomes(lambda cid: 5.0, now=NOW)
    assert summary["scanned"] == 0

def test_backfill_does_not_overwrite_existing_outcome():
    ws = _sheet_with_signal(age_days=20, price0=1.50)
    ws.rows[1][HEADER.index("outcome_7d")] = 5.0        # already recorded at day 7
    logger = AnomalyPaperLogger(ws)
    logger.backfill_outcomes(lambda cid: 3.00, now=NOW)   # would be +100% now
    d = dict(zip(HEADER, ws.rows[1]))
    assert d["outcome_7d"] == 5.0                        # preserved
    assert d["outcome_14d"] == 100.0                     # newly filled


def test_backfill_handles_missing_price_gracefully():
    ws = _sheet_with_signal(age_days=10)
    logger = AnomalyPaperLogger(ws)
    summary = logger.backfill_outcomes(lambda cid: None, now=NOW)
    d = dict(zip(HEADER, ws.rows[1]))
    assert d["outcome_7d"] == "" and d["status"] == "OPEN"
    assert summary["updated"] == 0


def test_backfill_empty_sheet():
    logger = AnomalyPaperLogger(FakeWorksheet([HEADER]))
    assert logger.backfill_outcomes(lambda cid: 1.0, now=NOW) == {"scanned": 0, "updated": 0, "closed": 0}
