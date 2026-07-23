"""Hyperliquid perps — free, key-less funding & open-interest snapshot.

Hyperliquid's ``/info`` endpoint returns funding rate, open interest and mark
price for *every* perp in a single POST (``metaAndAssetCtxs``), so one cached
call covers the whole watchlist — much cheaper than per-symbol funding requests,
and it lists many newer alts that Binance/OKX futures don't.

Funding is normalised to **percent** to match :mod:`wolf.market` and the other
funding sources; open interest is converted to USD (``size × mark price``).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from wolf.exchange.sources import split_quote

log = logging.getLogger("wolf.flow")


class HyperliquidPerps:
    name = "hyperliquid"

    def __init__(self, base_url: str = "https://api.hyperliquid.xyz",
                 timeout: float = 10.0, cache_ttl: float = 120.0,
                 session: Optional[requests.Session] = None) -> None:
        self._url = base_url.rstrip("/") + "/info"
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._session = session or requests.Session()
        self._cache: dict[str, dict] = {}
        self._cache_ts = 0.0

    # ── snapshot (cached) ──────────────────────────────────────────────
    def snapshot(self) -> dict[str, dict]:
        """Map coin → {funding_pct, oi_usd, mark_px}. Cached for ``cache_ttl``."""
        if self._cache and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cache
        try:
            resp = self._session.post(self._url, json={"type": "metaAndAssetCtxs"},
                                      timeout=self._timeout,
                                      headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("hyperliquid snapshot error: %s", exc)
            return self._cache  # serve stale rather than nothing
        parsed = self.parse(payload)
        if parsed:
            self._cache = parsed
            self._cache_ts = time.time()
        return parsed or self._cache

    @staticmethod
    def parse(payload) -> dict[str, dict]:
        """Parse ``[meta, assetCtxs]`` into a coin-keyed map."""
        if not (isinstance(payload, list) and len(payload) == 2):
            return {}
        meta, ctxs = payload
        universe = meta.get("universe") if isinstance(meta, dict) else None
        if not isinstance(universe, list) or not isinstance(ctxs, list):
            return {}
        out: dict[str, dict] = {}
        for coin_meta, ctx in zip(universe, ctxs):
            if not (isinstance(coin_meta, dict) and isinstance(ctx, dict)):
                continue
            name = coin_meta.get("name")
            if not name:
                continue
            mark = _f(ctx.get("markPx"))
            out[str(name).upper()] = {
                "funding_pct": _f(ctx.get("funding")) * 100,   # hourly rate → percent
                "oi_usd": _f(ctx.get("openInterest")) * mark,
                "mark_px": mark,
            }
        return out

    # ── per-symbol lookups (accepts BTCUSDT / BTC) ─────────────────────
    def _coin(self, symbol: str) -> str:
        base, _ = split_quote(symbol)
        return base.upper()

    def funding_rate(self, symbol: str) -> Optional[float]:
        row = self.snapshot().get(self._coin(symbol))
        return row["funding_pct"] if row else None

    def open_interest_usd(self, symbol: str) -> Optional[float]:
        row = self.snapshot().get(self._coin(symbol))
        return row["oi_usd"] if row and row["oi_usd"] > 0 else None


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
