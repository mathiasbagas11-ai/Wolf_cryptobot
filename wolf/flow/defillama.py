"""DefiLlama on-chain activity — free, key-less chain-rotation signals.

Feeds the CHAIN ROTATION ("kemana modal mengalir") and STABLECOIN ("dry powder")
sections of the flow report:

* per-chain DEX volume + 24h change  → where on-chain activity is concentrating
* aggregate stablecoin supply + change → dry-powder build-up / deployment

These are *aggregate* proxies for what the source tweet draws from Nansen's
wallet-level flows — honest approximations, never wallet-level claims.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("wolf.flow")

#: Chains the rotation section reports on (DefiLlama slugs).
DEFAULT_CHAINS = ("bsc", "base", "arbitrum", "ethereum", "solana")

#: Pretty labels for display.
CHAIN_LABELS = {"bsc": "BNB", "base": "Base", "arbitrum": "Arbitrum",
                "ethereum": "Ethereum", "solana": "Solana"}


@dataclass(frozen=True)
class ChainActivity:
    chain: str
    dex_volume_24h: float
    change_1d: float   # percent

    @property
    def label(self) -> str:
        return CHAIN_LABELS.get(self.chain, self.chain.capitalize())


@dataclass(frozen=True)
class StablecoinSupply:
    total_usd: float
    change_1d_pct: float
    change_7d_pct: float


class DefiLlamaClient:
    name = "defillama"

    def __init__(self, dex_base: str = "https://api.llama.fi",
                 stable_base: str = "https://stablecoins.llama.fi",
                 timeout: float = 10.0, session: Optional[requests.Session] = None) -> None:
        self._dex_base = dex_base.rstrip("/")
        self._stable_base = stable_base.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[object]:
        try:
            resp = self._session.get(url, params=params or {}, timeout=self._timeout,
                                     headers={"User-Agent": "wolf/1.0"})
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("defillama %s error: %s", url, exc)
            return None

    # ── raw fetch ──────────────────────────────────────────────────────
    def chain_activity(self, chains=DEFAULT_CHAINS) -> list[ChainActivity]:
        out: list[ChainActivity] = []
        for chain in chains:
            payload = self._get(
                f"{self._dex_base}/overview/dexs/{chain}",
                {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
            )
            parsed = self.parse_chain(chain, payload)
            if parsed is not None:
                out.append(parsed)
        return out

    def stablecoin_supply(self) -> Optional[StablecoinSupply]:
        payload = self._get(f"{self._stable_base}/stablecoincharts/all")
        return self.parse_stablecoins(payload)

    # ── pure parsing (testable without network) ────────────────────────
    @staticmethod
    def parse_chain(chain: str, payload) -> Optional[ChainActivity]:
        if not isinstance(payload, dict):
            return None
        vol = payload.get("total24h")
        if vol is None:
            return None
        return ChainActivity(
            chain=chain,
            dex_volume_24h=_f(vol),
            change_1d=_f(payload.get("change_1d")),
        )

    @staticmethod
    def parse_stablecoins(payload) -> Optional[StablecoinSupply]:
        if not isinstance(payload, list) or len(payload) < 2:
            return None

        def total(row) -> float:
            tc = row.get("totalCirculatingUSD") if isinstance(row, dict) else None
            if isinstance(tc, dict):
                return sum(_f(v) for v in tc.values())
            return _f(tc)

        latest = total(payload[-1])
        prev_1d = total(payload[-2])
        prev_7d = total(payload[-8]) if len(payload) >= 8 else total(payload[0])
        return StablecoinSupply(
            total_usd=latest,
            change_1d_pct=_pct(latest, prev_1d),
            change_7d_pct=_pct(latest, prev_7d),
        )


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct(now: float, then: float) -> float:
    return (now - then) / then * 100 if then else 0.0
