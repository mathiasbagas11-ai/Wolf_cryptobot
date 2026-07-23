"""Phase 6 — Paper trade logger.

Every anomaly signal scoring ≥ 55 is appended to a Google Sheet
(``Anomaly_Paper_Log``) so that, six weeks out, we have a *real* expectancy
dataset — win rate and average return of the setup, measured, not guessed.
Nothing here executes a trade; it only records.

Two jobs:

* :meth:`AnomalyPaperLogger.log_signals` — write one row per fresh signal
  (``status=OPEN``, outcome columns left blank).
* :meth:`AnomalyPaperLogger.backfill_outcomes` — daily; for each OPEN row fill
  ``outcome_7d / 14d / 30d`` as % change from ``price_at_signal`` once that many
  days have elapsed, and flip ``status`` to ``CLOSED`` past 30 days.

The Sheet access is injected as a duck-typed worksheet (``get_all_values`` /
``append_row`` / ``update_cell``) so the row-building and backfill logic
unit-tests with an in-memory fake — no gspread, no network. :func:`open_worksheet`
builds the real gspread worksheet from the service-account credentials that live
in the deployment env.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("wolf.anomaly")

SHEET_NAME = "Anomaly_Paper_Log"
MIN_LOG_SCORE = 55

#: Column order — must stay in sync with the Sheet header (backfill indexes by name).
HEADER = [
    "timestamp", "symbol", "coin_id", "score",
    "score_A", "score_B", "score_C", "score_D",
    "price_at_signal", "l1", "l2", "l3", "invalidation", "tp1", "tp2",
    "mcap", "fdv_mc", "turnover", "atr_ratio", "vol_ratio", "range_position",
    "market_verdict", "in_dca_sleeve", "status",
    "outcome_7d", "outcome_14d", "outcome_30d", "notes",
]

_OUTCOME_WINDOWS = [(7, "outcome_7d"), (14, "outcome_14d"), (30, "outcome_30d")]
_CLOSE_AFTER_DAYS = 30


# ── pure row building ──────────────────────────────────────────────────────
def build_row(coin: dict, market_verdict: str, now: datetime) -> list:
    """Assemble one Sheet row (in HEADER order) from an enriched coin dict.

    ``coin`` carries the Phase-3 score/components/metrics plus ``current_price``,
    ``market_cap`` and (optionally) the Phase-4 ``ladder``.
    """
    comps = coin.get("components", {})
    metrics = coin.get("metrics", {})
    ladder = coin.get("ladder") or {}
    price = coin.get("current_price")
    atr_ratio = metrics.get("atr_ratio")        # ATR(14)/ATR(60) contraction ratio

    return [
        now.astimezone(timezone.utc).isoformat(),
        str(coin.get("symbol", "")).upper(),
        str(coin.get("id", coin.get("coin_id", ""))),
        _num(coin.get("score")),
        _num(comps.get("volatility_contraction")),
        _num(comps.get("volume_anomaly")),
        _num(comps.get("structure_position")),
        _num(comps.get("supply_health")),
        _num(price, 8),
        _num(ladder.get("l1"), 8),
        _num(ladder.get("l2"), 8),
        _num(ladder.get("l3"), 8),
        _num(ladder.get("invalidation"), 8),
        _num(ladder.get("tp1"), 8),
        _num(ladder.get("tp2"), 8),
        _num(coin.get("market_cap", coin.get("mcap"))),
        _num(metrics.get("fdv_mc"), 3),
        _num(metrics.get("turnover"), 4),
        _num(atr_ratio, 4),
        _num(metrics.get("volume_ratio"), 3),
        _num(metrics.get("range_position"), 3),
        str(market_verdict or "").upper(),
        "TRUE" if coin.get("in_dca_sleeve") else "FALSE",
        "OPEN",
        "", "", "",                     # outcome_7d / 14d / 30d — backfilled later
        "",                             # notes
    ]


class AnomalyPaperLogger:
    """Append signals to, and backfill outcomes on, the paper-log worksheet."""

    def __init__(self, worksheet, *, min_score: int = MIN_LOG_SCORE) -> None:
        self._ws = worksheet
        self._min_score = min_score

    def ensure_header(self) -> None:
        """Write the header row if the sheet is empty (idempotent)."""
        try:
            values = self._ws.get_all_values()
        except Exception:                       # noqa: BLE001 — treat as empty
            values = []
        if not values or not any(values[0]):
            self._ws.append_row(HEADER)

    def log_signals(self, coins: list, market_verdict: str, *,
                    now: Optional[datetime] = None) -> int:
        """Append a row for every non-flagged coin scoring ≥ ``min_score``.

        Returns the number of rows written. The display gate (watchlist vs
        picks) does not apply here — every qualifying signal is logged so the
        expectancy dataset is complete.
        """
        now = now or datetime.now(timezone.utc)
        self.ensure_header()
        written = 0
        for coin in coins:
            if coin.get("flagged"):
                continue
            if _f(coin.get("score")) < self._min_score:
                continue
            try:
                self._ws.append_row(build_row(coin, market_verdict, now))
                written += 1
            except Exception:                   # noqa: BLE001 — one bad row never breaks the batch
                log.exception("anomaly log: failed to append %s", coin.get("symbol"))
        return written

    def open_coin_ids(self) -> list:
        """Distinct coin_ids of rows still ``status=OPEN`` (for a batched price fetch)."""
        values = self._ws.get_all_values()
        if len(values) < 2:
            return []
        col = {name: i for i, name in enumerate(values[0])}
        ids: list[str] = []
        for row in values[1:]:
            if _cell(row, col, "status") == "OPEN":
                cid = _cell(row, col, "coin_id")
                if cid and cid not in ids:
                    ids.append(cid)
        return ids

    def backfill_outcomes(self, price_lookup: Callable[[str], Optional[float]], *,
                          now: Optional[datetime] = None) -> dict:
        """Fill elapsed outcome windows for OPEN rows; CLOSE rows past 30 days.

        ``price_lookup(coin_id)`` returns the coin's current USD price (or None).
        Returns a summary ``{"scanned", "updated", "closed"}``.
        """
        now = now or datetime.now(timezone.utc)
        values = self._ws.get_all_values()
        if len(values) < 2:
            return {"scanned": 0, "updated": 0, "closed": 0}

        col = {name: i for i, name in enumerate(values[0])}
        summary = {"scanned": 0, "updated": 0, "closed": 0}

        for sheet_row, row in enumerate(values[1:], start=2):   # 1-indexed; header is row 1
            if _cell(row, col, "status") != "OPEN":
                continue
            summary["scanned"] += 1

            ts = _parse_iso(_cell(row, col, "timestamp"))
            price0 = _f(_cell(row, col, "price_at_signal"))
            coin_id = _cell(row, col, "coin_id")
            if ts is None or price0 <= 0 or not coin_id:
                continue

            age_days = (now - ts).total_seconds() / 86_400
            price_now = price_lookup(coin_id)
            row_changed = False

            if price_now is not None and price0 > 0:
                pct = round((price_now - price0) / price0 * 100, 2)
                for win_days, colname in _OUTCOME_WINDOWS:
                    if age_days >= win_days and not _cell(row, col, colname):
                        self._ws.update_cell(sheet_row, col[colname] + 1, pct)
                        row_changed = True

            if age_days >= _CLOSE_AFTER_DAYS:
                self._ws.update_cell(sheet_row, col["status"] + 1, "CLOSED")
                summary["closed"] += 1
                row_changed = True

            if row_changed:
                summary["updated"] += 1

        return summary


# ── gspread wiring (lazy; only touched in production) ──────────────────────
def open_worksheet(credentials, sheet_name: str = SHEET_NAME):
    """Open (or create) the paper-log worksheet via gspread.

    ``credentials`` may be a path to a service-account JSON file, a raw JSON
    string, or a dict. Raises if gspread isn't installed or auth fails —
    callers should guard so a Sheets outage never breaks the scan.
    """
    import gspread  # lazy: keeps the package importable without the optional dep

    creds = _load_credentials(credentials)
    client = gspread.service_account_from_dict(creds)
    try:
        spreadsheet = client.open(sheet_name)
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(sheet_name)
    return spreadsheet.sheet1


def _load_credentials(credentials) -> dict:
    if isinstance(credentials, dict):
        return credentials
    if isinstance(credentials, str):
        if os.path.exists(credentials):
            with open(credentials, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return json.loads(credentials)
    raise ValueError("credentials must be a dict, JSON string, or file path")


def logger_from_env(*, worksheet=None) -> Optional["AnomalyPaperLogger"]:
    """Build a logger from env vars, or None if paper logging isn't configured.

    ``ANOMALY_PAPER_ENABLED`` gates it; ``GOOGLE_SHEETS_CREDENTIALS`` holds the
    service-account JSON (raw or path); ``ANOMALY_SHEET_NAME`` overrides the
    sheet title. A worksheet may be injected (tests) to skip gspread entirely.
    """
    if os.getenv("ANOMALY_PAPER_ENABLED", "false").strip().lower() not in ("1", "true", "yes"):
        return None
    if worksheet is not None:
        return AnomalyPaperLogger(worksheet)
    creds = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "").strip()
    if not creds:
        log.warning("anomaly paper log enabled but GOOGLE_SHEETS_CREDENTIALS is unset")
        return None
    sheet_name = os.getenv("ANOMALY_SHEET_NAME", SHEET_NAME)
    try:
        ws = open_worksheet(creds, sheet_name)
    except Exception:                           # noqa: BLE001 — never break boot on Sheets issues
        log.exception("anomaly paper log: could not open worksheet")
        return None
    return AnomalyPaperLogger(ws)


# ── helpers ────────────────────────────────────────────────────────────────
def _num(v, ndigits: int = 2):
    """Round a number for the sheet; blank string for None/non-numeric."""
    if v is None:
        return ""
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return ""


def _f(v) -> float:
    try:
        if v in (None, ""):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _cell(row: list, col: dict, name: str) -> str:
    idx = col.get(name)
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
