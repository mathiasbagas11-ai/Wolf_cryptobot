"""Phase 7 — Scan orchestrator.

Ties the pipeline together: universe (Phase 1) → per-coin OHLC (Phase 2) →
score (Phase 3) → entry ladder (Phase 4), then renders the section (Phase 5)
and logs the signals (Phase 6). This is the single object the news bot's entry
point calls to obtain the ANOMALY SCANNER section.

Rate-limit / runtime discipline (CoinGecko free tier, Railway < 8 min/cycle):

* only the top ``scan_limit`` coins (universe is volume-ordered) are scanned;
* a wall-clock ``time_budget_sec`` stops the loop early with partial results
  rather than blowing the cycle budget — aggressive 4h OHLC caching means most
  cycles re-scan for free;
* a single coin's fetch/score failure is swallowed so it never aborts the scan.

PAPER MODE only — the scanner never places an order. All dependencies (universe,
ohlc, score, ladder, logger, clock) are injected so it unit-tests offline.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from anomaly.entry import build_ladder
from anomaly.formatter import format_anomaly_section
from anomaly.ohlc import fetch_ohlc, fetch_prices
from anomaly.scoring import score_coin
from anomaly.universe import build_universe

log = logging.getLogger("wolf.anomaly")

try:                                    # monotonic clock, injectable for tests
    from time import monotonic as _default_clock
except ImportError:                     # pragma: no cover
    from time import time as _default_clock


class AnomalyScanner:
    def __init__(
        self,
        *,
        min_score: int = 55,
        max_picks: int = 3,
        scan_limit: int = 50,
        time_budget_sec: float = 360.0,
        paper_mode: bool = True,
        logger=None,
        universe_fn: Callable[[], list] = build_universe,
        ohlc_fn: Callable[..., object] = fetch_ohlc,
        score_fn: Callable[..., dict] = score_coin,
        ladder_fn: Callable[..., dict] = build_ladder,
        prices_fn: Callable[[list], dict] = fetch_prices,
        clock: Callable[[], float] = _default_clock,
    ) -> None:
        self._min_score = min_score
        self._max_picks = max_picks
        self._scan_limit = scan_limit
        self._time_budget = time_budget_sec
        self._paper_mode = paper_mode      # guard: True → never execute (always, here)
        self._logger = logger
        self._universe_fn = universe_fn
        self._ohlc_fn = ohlc_fn
        self._score_fn = score_fn
        self._ladder_fn = ladder_fn
        self._prices_fn = prices_fn
        self._clock = clock

    # ── the pipeline ───────────────────────────────────────────────────────
    def scan(self, market_verdict: str) -> dict:
        """Run universe → OHLC → score → ladder over the budgeted coin set.

        Returns ``{"scored", "flagged", "scanned", "universe"}``. ``scored``
        coins are enriched with ``current_price``, ``market_cap`` and (when not
        flagged) a ``ladder`` — exactly what the formatter and logger expect.
        """
        universe = self._universe_fn() or []
        coins = universe[: self._scan_limit]
        scored: list[dict] = []
        flagged: list[dict] = []
        scanned = 0
        start = self._clock()

        for coin in coins:
            if self._clock() - start > self._time_budget:
                log.warning("anomaly scan: time budget hit after %d coins", scanned)
                break
            try:
                res = self._scan_one(coin)
            except Exception:               # one bad coin never aborts the scan
                log.debug("anomaly scan: failed on %s", coin.get("id"), exc_info=True)
                continue
            if res is None:
                continue
            scanned += 1
            (flagged if res.get("flagged") else scored).append(res)

        scored.sort(key=lambda c: c.get("score", 0), reverse=True)
        return {"scored": scored, "flagged": flagged,
                "scanned": scanned, "universe": len(universe)}

    def _scan_one(self, coin: dict) -> Optional[dict]:
        coin_id = coin.get("id")
        if not coin_id:
            return None
        df = self._ohlc_fn(coin_id)
        res = self._score_fn(df, coin)
        res["current_price"] = coin.get("current_price")
        res["market_cap"] = coin.get("market_cap")
        if not res.get("flagged"):
            price = coin.get("current_price")
            if price:
                res["ladder"] = self._ladder_fn(df, price)
        return res

    # ── section rendering (scan + log + format) ────────────────────────────
    def build_section(self, market_verdict: str) -> str:
        """Scan, log the qualifying signals, and return the rendered section."""
        result = self.scan(market_verdict)
        self._log_signals(result["scored"], market_verdict)
        return format_anomaly_section(
            result["scored"], result["flagged"],
            market_verdict=market_verdict, max_display=self._max_picks,
        )

    def _log_signals(self, scored: list, market_verdict: str) -> None:
        if self._logger is None:
            return
        try:
            n = self._logger.log_signals(scored, market_verdict)
            if n:
                log.info("anomaly paper log: wrote %d signal(s)", n)
        except Exception:                   # logging must never break the report
            log.exception("anomaly paper log: write failed")

    # ── daily outcome backfill ─────────────────────────────────────────────
    def run_backfill(self) -> dict:
        """Daily job: fetch prices for OPEN signals and fill their outcomes."""
        if self._logger is None:
            return {"scanned": 0, "updated": 0, "closed": 0}
        ids = self._logger.open_coin_ids()
        prices = self._prices_fn(ids) if ids else {}
        return self._logger.backfill_outcomes(lambda cid: prices.get(cid))
