"""Universe selection — which symbols the screener scans each cycle.

The original universe was a hardcoded list of 15 majors, so the bot only ever
looked at the same coins and never saw meme coins or smaller ecosystems heating
up. :class:`UniverseProvider` instead ranks the whole market by 24h quote volume
(a single ``get_market_overview`` call) and scans the most liquid pairs, with a
stable core of majors always included. Liquidity is the gate — high quote volume
means tight spreads and tradeable fills, regardless of which ecosystem a coin is
from — so movers rotate in and out naturally as the market shifts.

**The mover lane.** Ranking by 24h volume alone is a *lagging* filter, and it
lags in exactly the place the bot cares about most. A coin enters the top-N by
volume only *because* it already pumped, which puts the volume lane in direct
contradiction with the detector that is supposed to catch a move early:
``PREPUMP`` looks for quiet accumulation — low volume by definition — on a
symbol the volume lane cannot see until the accumulation is over. That is why a
token can run +127% in a day without the bot ever having evaluated a single one
of its candles.

The second lane fixes the reachability half of that. It applies a *lower*
liquidity floor and ranks by 24h price change instead of volume, so a mid-cap
already moving gets scanned while the move is young rather than after it has
grown into a market-wide volume leader. It is still reactive — the 24h snapshot
carries no volume baseline to compare against, so genuine pre-move accumulation
is not detectable from this call alone — but it moves the entry point from
"after the pump" to "early in the pump", which is where the detectors can
actually do their job. Both directions qualify: a token down 20% is a candidate
for the short detectors the same way one up 20% is for the long side.
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
        mover_lane: bool = True,
        mover_top_n: int = 15,
        mover_min_quote_volume: float = 3_000_000,
        mover_min_change_pct: float = 8.0,
    ) -> None:
        self._client = client
        self._core = tuple(core)
        self._top_n = top_n
        self._min_vol = min_quote_volume
        self._quote = quote
        self._mover_lane = mover_lane
        self._mover_top_n = mover_top_n
        self._mover_min_vol = mover_min_quote_volume
        self._mover_min_change = mover_min_change_pct

    def _is_tradeable(self, symbol: str) -> bool:
        if not symbol.endswith(self._quote):
            return False
        base = symbol[: -len(self._quote)]
        return base not in EXCLUDED_BASES

    def _movers(self, rows: Sequence[dict], taken: set[str]) -> list[str]:
        """Symbols already moving, above a lower liquidity floor.

        Ranked by the *size* of the 24h move regardless of sign, so the short
        detectors get their candidates from the same lane as the long ones.
        ``taken`` holds everything the core and volume lanes already claimed —
        a symbol is scanned once per cycle no matter how many lanes select it.
        """
        if not self._mover_lane or self._mover_top_n <= 0:
            return []
        candidates = [
            r for r in rows
            if self._is_tradeable(r.get("symbol", ""))
            and r["symbol"] not in taken
            and r.get("quote_volume", 0) >= self._mover_min_vol
            and abs(r.get("change_pct", 0) or 0) >= self._mover_min_change
        ]
        ranked = sorted(
            candidates, key=lambda r: abs(r.get("change_pct", 0) or 0), reverse=True
        )
        return [r["symbol"] for r in ranked[: self._mover_top_n]]

    def symbols(self) -> list[str]:
        """Return the symbols to scan: core majors + volume leaders + movers.

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
        movers = self._movers(rows, core_set | set(top))
        if movers:
            log.info(
                "Mover lane added %d symbol(s) below the volume cut: %s",
                len(movers), ", ".join(movers),
            )
        return list(self._core) + top + movers
