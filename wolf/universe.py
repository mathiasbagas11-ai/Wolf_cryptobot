"""Universe selection — which symbols the screener scans each cycle.

The original universe was a hardcoded list of 15 majors, so the bot only ever
looked at the same coins and never saw meme coins or smaller ecosystems heating
up. :class:`UniverseProvider` instead ranks the whole market by 24h quote volume
(a single ``get_market_overview`` call) and scans the most liquid pairs, with a
stable core of majors always included. Liquidity is the gate — high quote volume
means tight spreads and tradeable fills, regardless of which ecosystem a coin is
from — so movers rotate in and out naturally as the market shifts.
"""

from __future__ import annotations

import logging
from typing import Sequence

log = logging.getLogger("wolf.universe")

# Always scanned: deep-liquidity majors that also anchor the regime read.
CORE_MAJORS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")

# Bases excluded from the dynamic tail: stablecoins (no directional edge) and
# wrapped/pegged assets that just track another coin.
EXCLUDED_BASES: frozenset[str] = frozenset({
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDD", "EUR", "GBP",
    "AEUR", "EURI", "WBTC", "WBETH",
})


class UniverseProvider:
    """Builds the scan universe from a live market-volume snapshot."""

    def __init__(
        self,
        client,
        core: Sequence[str] = CORE_MAJORS,
        top_n: int = 30,
        min_quote_volume: float = 10_000_000,
        quote: str = "USDT",
    ) -> None:
        self._client = client
        self._core = tuple(core)
        self._top_n = top_n
        self._min_vol = min_quote_volume
        self._quote = quote

    def _is_tradeable(self, symbol: str) -> bool:
        if not symbol.endswith(self._quote):
            return False
        base = symbol[: -len(self._quote)]
        return base not in EXCLUDED_BASES

    def symbols(self) -> list[str]:
        """Return the symbols to scan: core majors + top volume leaders.

        Falls back to the core majors alone if the market snapshot is empty or
        the request fails, so a screening cycle is never starved of symbols.
        """
        try:
            rows = self._client.get_market_overview()
        except Exception:
            log.warning("Universe overview fetch failed — using core majors", exc_info=True)
            rows = []

        core_set = set(self._core)
        liquid = [
            r for r in rows
            if self._is_tradeable(r.get("symbol", ""))
            and r.get("quote_volume", 0) >= self._min_vol
            and r["symbol"] not in core_set  # core is added unconditionally below
        ]
        ranked = sorted(liquid, key=lambda r: r.get("quote_volume", 0), reverse=True)
        # ``top_n`` counts *additional* movers beyond the core majors.
        top = [r["symbol"] for r in ranked[: self._top_n]]
        return list(self._core) + top
