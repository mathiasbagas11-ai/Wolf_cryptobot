"""Market-wide flow snapshot: macro, dry powder, chain rotation, screen candidates.

Sections 1–3 and 6 of the Flow Intelligence digest need market-level data that
is identical for every symbol — total market cap and dominance, aggregate
stablecoin supply, per-chain DEX volume, and a token screen. This collector
fetches all of it once and persists it, so the reporter can be a pure function of
the StateStore.

It deliberately owns no analysis. Deciding which tokens are worth listing is the
reporter's job (and its filters are what the previous report got wrong); this
just records what the sources said, with the timestamp readers need to judge how
stale it is.

The HTTP clients are the ones already in :mod:`wolf.flow` — reusing them means
one parser per endpoint rather than a second copy that can drift.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from wolf.flow.coingecko import CoinGeckoClient, GlobalMetrics, TokenMetrics
from wolf.flow.defillama import ChainActivity, DefiLlamaClient, StablecoinSupply

log = logging.getLogger("wolf.onchain.macro")

#: StateStore document this collector owns.
STATE_KEY = "flow_macro"


class MacroFlowCollector:
    """Fetches the market-wide flow inputs and persists one snapshot."""

    name = "flow_macro"

    def __init__(
        self,
        store,
        *,
        coingecko: Optional[CoinGeckoClient] = None,
        defillama: Optional[DefiLlamaClient] = None,
        markets_limit: int = 60,
    ) -> None:
        self._store = store
        self._cg = coingecko or CoinGeckoClient()
        self._llama = defillama or DefiLlamaClient()
        self._markets_limit = markets_limit

    def collect(self) -> dict:
        """One pass over every market-wide source, persisted under ``STATE_KEY``.

        Each source is optional: a CoinGecko outage costs the macro section, not
        the chain-rotation section. The reporter renders whatever survived.
        """
        markets = self._cg.top_markets(limit=self._markets_limit)
        global_metrics = self._cg.global_data()
        chains = self._llama.chain_activity()
        stablecoin = self._llama.stablecoin_supply()

        doc = {
            "global": _global_doc(global_metrics),
            "stablecoin": _stablecoin_doc(stablecoin),
            "chains": [_chain_doc(c) for c in chains],
            "markets": [_token_doc(t) for t in markets],
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._store.write(STATE_KEY, doc)
        log.info("Macro flow snapshot: %d market(s), %d chain(s), global=%s, stables=%s",
                 len(doc["markets"]), len(doc["chains"]),
                 doc["global"] is not None, doc["stablecoin"] is not None)
        return doc


# ── payload → plain JSON (pure) ───────────────────────────────────────────
def _global_doc(g: Optional[GlobalMetrics]) -> Optional[dict]:
    if g is None:
        return None
    return {
        "btc_dominance": g.btc_dominance,
        "usdt_dominance": g.usdt_dominance,
        "total_market_cap": g.total_market_cap,
        "market_cap_change_24h": g.market_cap_change_24h,
    }


def _stablecoin_doc(s: Optional[StablecoinSupply]) -> Optional[dict]:
    if s is None:
        return None
    return {
        "total_usd": s.total_usd,
        "change_1d_pct": s.change_1d_pct,
        "change_7d_pct": s.change_7d_pct,
    }


def _chain_doc(c: ChainActivity) -> dict:
    return {
        "chain": c.chain,
        "label": c.label,
        "dex_volume_24h": c.dex_volume_24h,
        "change_1d": c.change_1d,
    }


def _token_doc(t: TokenMetrics) -> dict:
    return {
        "symbol": t.symbol,
        "name": t.name,
        "price": t.price,
        "change_24h": t.change_24h,
        "market_cap": t.market_cap,
        "fdv": t.fdv,
        "volume_24h": t.volume_24h,
        "ath_change_pct": t.ath_change_pct,
    }
