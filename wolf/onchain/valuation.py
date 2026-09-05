"""On-chain valuation — the fundamental layer under the technical one.

Answers a question no candle can: *is this thing cheap or expensive, and is the
float about to double?* Ported from the previous bot's ``onchain_valuation.py``,
restructured to Wolf's conventions:

* **Pure logic split from I/O.** :func:`compute_valuation_metrics`,
  :func:`assess_valuation` and :func:`build_valuation_brief` are pure functions
  of already-fetched payloads, mirroring ``parse_klines`` in
  :mod:`wolf.exchange.sources`; only :meth:`ValuationCollector.fetch` touches the
  network. Tests exercise the judgement without a socket.
* **Instance cache, not a module global.** The original kept a module-level
  ``_CACHE`` dict, so two collectors (or two tests) shared one cache and neither
  could be reset. The TTL cache lives on the instance here.
* **Explicit 429 handling.** A 15-symbol universe on a 10-minute cycle is 90
  CoinGecko calls an hour uncached — the public API will not wear that. The 1
  hour TTL cuts it to ~15, and a 429 trips a backoff window in which the
  collector stops calling instead of hammering a limiter that is already angry.

Metrics computed: FDV ratio (unlock overhang), volume/market-cap (turnover),
circulating supply %, distance from ATH (cycle position), market-cap/TVL and
30-day TVL trend for DeFi tokens (valuation vs real usage).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import requests

from wolf.exchange.sources import split_quote

log = logging.getLogger("wolf.onchain.valuation")

#: StateStore document this collector owns.
STATE_KEY = "onchain_valuation"

# ── assessment thresholds ─────────────────────────────────────────────────
FDV_RATIO_LOW = 0.30       # < 30% of supply circulating → heavy unlock overhang
FDV_RATIO_HIGH = 0.80      # > 80% circulating → dilution risk largely spent
VOL_MCAP_HIGH = 0.15       # turnover strong enough to enter and exit
VOL_MCAP_LOW = 0.02        # thin interest, illiquidity risk
ATH_NEAR = -8.0            # within 8% of ATH → price discovery / euphoria
ATH_DEEP = -85.0           # 85%+ below ATH → deeply depressed valuation
MCAP_TVL_CHEAP = 1.0       # valued below the capital it actually holds
MCAP_TVL_RICH = 8.0        # speculative premium over real usage
TVL_TREND_UP = 15.0        # percent over 30 days
TVL_TREND_DOWN = -15.0

BIAS_SUPPORTS_LONG = "SUPPORTS_LONG"
BIAS_SUPPORTS_SHORT = "SUPPORTS_SHORT"
BIAS_CAUTION = "CAUTION"
BIAS_NEUTRAL = "NEUTRAL"

_LONG_DIRECTIONS = frozenset({"LONG", "PUMP", "PREPUMP", "BUY"})

#: Symbol → CoinGecko coin id. An explicit map avoids the ticker collisions that
#: ``/search`` resolves wrongly (several coins answer to "SOL" or "OP").
CG_ID_OVERRIDE: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano",
    "AVAX": "avalanche-2", "DOGE": "dogecoin", "LINK": "chainlink",
    "DOT": "polkadot", "MATIC": "matic-network", "POL": "polygon-ecosystem-token",
    "UNI": "uniswap", "AAVE": "aave", "LDO": "lido-dao",
    "ARB": "arbitrum", "OP": "optimism", "INJ": "injective-protocol",
    "SUI": "sui", "SEI": "sei-network", "TIA": "celestia",
    "NEAR": "near", "APT": "aptos", "FIL": "filecoin",
    "RNDR": "render-token", "RENDER": "render-token", "FET": "fetch-ai",
    "TAO": "bittensor", "WLD": "worldcoin-wld", "TON": "the-open-network",
    "LTC": "litecoin", "ATOM": "cosmos", "ICP": "internet-computer",
    "TRX": "tron", "BCH": "bitcoin-cash", "ETC": "ethereum-classic",
    "HBAR": "hedera-hashgraph", "PEPE": "pepe", "SHIB": "shiba-inu",
    "WIF": "dogwifcoin", "BONK": "bonk", "JUP": "jupiter-exchange-solana",
    "PENDLE": "pendle", "ENA": "ethena", "STX": "blockstack",
}

#: DeFi token → DefiLlama protocol slug, for the TVL-based metrics. Best effort:
#: a symbol that is not here simply has no TVL dimension, which is correct — an
#: L1 or a meme coin has no protocol TVL to compare its market cap against.
LLAMA_SLUG: dict[str, str] = {
    "UNI": "uniswap", "AAVE": "aave", "LDO": "lido",
    "MKR": "makerdao", "CRV": "curve-dex", "SNX": "synthetix",
    "COMP": "compound-finance", "SUSHI": "sushi", "GMX": "gmx",
    "DYDX": "dydx", "PENDLE": "pendle", "INJ": "injective",
    "JUP": "jupiter", "RAY": "raydium", "CAKE": "pancakeswap",
    "ENA": "ethena", "ETHFI": "ether-fi", "MORPHO": "morpho",
}


def base_symbol(symbol: str) -> str:
    """``BTCUSDT`` → ``BTC``.

    Uses :func:`~wolf.exchange.sources.split_quote` rather than the old
    ``.replace("USDT", "")``, which mangled any base whose own name contains the
    quote ("USDTB" became "B", and "SUSDT" — sUSD's pair — became "S").
    """
    base, _ = split_quote(symbol.upper().strip())
    return base


# ── pure logic (unit-tested without network) ──────────────────────────────
def compute_valuation_metrics(raw: Optional[dict], tvl_data: Optional[dict] = None) -> dict:
    """Derive valuation metrics from a CoinGecko markets row (+ optional TVL).

    Every field is defensive against ``None``/zero: a missing denominator yields
    ``None`` for that ratio rather than a fabricated number, so downstream code
    can tell "no data" from "data says zero".
    """
    raw = raw or {}
    mcap = _f(raw.get("market_cap"))
    fdv = _f(raw.get("fully_diluted_valuation"))
    vol = _f(raw.get("total_volume"))
    circ = _f(raw.get("circulating_supply"))
    total = _f(raw.get("total_supply"))

    tvl: Optional[float] = None
    mcap_tvl: Optional[float] = None
    tvl_chg_30d: Optional[float] = None
    if tvl_data:
        tvl_val = _f(tvl_data.get("tvl"))
        if tvl_val > 0:
            tvl = tvl_val
            if mcap > 0:
                mcap_tvl = mcap / tvl_val
        tvl_chg_30d = _opt_f(tvl_data.get("tvl_chg_30d"))

    return {
        "mcap": mcap,
        "fdv": fdv,
        # 1.0 = fully circulating (no unlocks left); 0.2 = 80% still locked.
        "fdv_ratio": (mcap / fdv) if fdv > 0 else None,
        "vol_mcap": (vol / mcap) if mcap > 0 else None,
        "circ_pct": (circ / total * 100) if total > 0 else None,
        "ath_chg_pct": _opt_f(raw.get("ath_change_percentage")),
        "chg_7d": _opt_f(raw.get("price_change_percentage_7d_in_currency")),
        "chg_30d": _opt_f(raw.get("price_change_percentage_30d_in_currency")),
        "tvl": tvl,
        "mcap_tvl": mcap_tvl,
        "tvl_chg_30d": tvl_chg_30d,
    }


def assess_valuation(metrics: dict, direction: Optional[str] = None) -> dict:
    """Turn metrics into a bias plus the bull/bear notes behind it.

    Called two ways, and the distinction is the whole point:

    * ``direction=None`` — what the fundamentals *say*, independent of any trade:
      ``SUPPORTS_LONG`` / ``SUPPORTS_SHORT`` / ``NEUTRAL``. This is what the
      collector persists, because a stored snapshot has no trade attached to it.
    * ``direction="LONG"``/``"SHORT"`` — the same reading judged *against a
      proposed trade*, which adds ``CAUTION``: the fundamentals point the other
      way. Only a caller holding a candidate can ask this.

    Notes are returned verbatim in both cases; only the label changes.
    """
    bull: list[str] = []
    bear: list[str] = []

    fdv_ratio = metrics.get("fdv_ratio")
    if fdv_ratio is not None:
        if fdv_ratio < FDV_RATIO_LOW:
            bear.append(
                f"Cuma {fdv_ratio * 100:.0f}% supply beredar — overhang unlock besar (dilusi menekan harga)."
            )
        elif fdv_ratio > FDV_RATIO_HIGH:
            bull.append(f"{fdv_ratio * 100:.0f}% supply sudah beredar — risiko dilusi unlock kecil.")

    vol_mcap = metrics.get("vol_mcap")
    if vol_mcap is not None:
        if vol_mcap > VOL_MCAP_HIGH:
            bull.append(f"Turnover tinggi (vol/mcap {vol_mcap:.2f}) — minat & likuiditas kuat.")
        elif vol_mcap < VOL_MCAP_LOW:
            bear.append(f"Turnover rendah (vol/mcap {vol_mcap:.3f}) — minat tipis, rawan ilikuid.")

    ath = metrics.get("ath_chg_pct")
    if ath is not None:
        if ath > ATH_NEAR:
            bear.append(f"Cuma {abs(ath):.0f}% dari ATH — zona price discovery, R:R LONG memburuk.")
        elif ath < ATH_DEEP:
            bull.append(f"{abs(ath):.0f}% di bawah ATH — valuasi tertekan dalam, ruang pemulihan besar.")

    mcap_tvl = metrics.get("mcap_tvl")
    if mcap_tvl is not None:
        if mcap_tvl < MCAP_TVL_CHEAP:
            bull.append(f"MCap/TVL {mcap_tvl:.2f} (<1) — murah relatif terhadap TVL nyata.")
        elif mcap_tvl > MCAP_TVL_RICH:
            bear.append(f"MCap/TVL {mcap_tvl:.1f} — premium spekulatif tinggi vs penggunaan riil.")

    tvl_chg = metrics.get("tvl_chg_30d")
    if tvl_chg is not None:
        if tvl_chg > TVL_TREND_UP:
            bull.append(f"TVL naik {tvl_chg:+.0f}% (30h) — protokol bertumbuh, modal masuk.")
        elif tvl_chg < TVL_TREND_DOWN:
            bear.append(f"TVL turun {tvl_chg:+.0f}% (30h) — modal keluar, fundamental melemah.")

    bias = _bias_for(len(bull), len(bear), direction)
    return {
        "bias": bias,
        "bull_notes": bull,
        "bear_notes": bear,
        "headline": HEADLINES[bias],
    }


HEADLINES = {
    BIAS_SUPPORTS_LONG: "Fundamental mendukung LONG",
    BIAS_SUPPORTS_SHORT: "Fundamental mendukung SHORT",
    BIAS_CAUTION: "Fundamental berlawanan dengan arah trade",
    BIAS_NEUTRAL: "Fundamental netral",
}


def _bias_for(n_bull: int, n_bear: int, direction: Optional[str]) -> str:
    if n_bull == n_bear:
        return BIAS_NEUTRAL
    supports = BIAS_SUPPORTS_LONG if n_bull > n_bear else BIAS_SUPPORTS_SHORT
    if direction is None:
        return supports
    wants_long = direction.upper() in _LONG_DIRECTIONS
    aligned = (supports == BIAS_SUPPORTS_LONG) == wants_long
    return supports if aligned else BIAS_CAUTION


def build_valuation_brief(metrics: dict, assessment: dict, coin: str) -> str:
    """One compact block of facts + notes, for injection into the AI debate."""
    if not assessment:
        return ""
    lines = [f"VALUASI ON-CHAIN {coin} — {assessment.get('headline', '')}:"]

    facts: list[str] = []
    if metrics.get("mcap"):
        facts.append(f"MCap ${metrics['mcap'] / 1e6:,.0f}M")
    if metrics.get("fdv_ratio") is not None:
        facts.append(f"FDV ratio {metrics['fdv_ratio']:.2f}")
    if metrics.get("vol_mcap") is not None:
        facts.append(f"Vol/MCap {metrics['vol_mcap']:.3f}")
    if metrics.get("ath_chg_pct") is not None:
        facts.append(f"{metrics['ath_chg_pct']:+.0f}% dari ATH")
    if metrics.get("mcap_tvl") is not None:
        facts.append(f"MCap/TVL {metrics['mcap_tvl']:.2f}")
    if metrics.get("tvl_chg_30d") is not None:
        facts.append(f"TVL 30h {metrics['tvl_chg_30d']:+.0f}%")
    if facts:
        lines.append("  " + " | ".join(facts))

    lines += [f"  🐂 {n}" for n in assessment.get("bull_notes", [])[:3]]
    lines += [f"  🐻 {n}" for n in assessment.get("bear_notes", [])[:3]]
    return "\n".join(lines)


def parse_markets_row(payload: Any) -> Optional[dict]:
    """Pull the single row out of a ``/coins/markets?ids=`` response."""
    if not isinstance(payload, list) or not payload:
        return None
    return payload[0] if isinstance(payload[0], dict) else None


def parse_tvl(payload: Any) -> Optional[dict]:
    """Extract current TVL and its 30-day change from a DefiLlama protocol doc."""
    if not isinstance(payload, dict):
        return None
    series = payload.get("tvl")
    tvl = 0.0
    chg_30d: Optional[float] = None
    if isinstance(series, list) and series:
        last = series[-1]
        tvl = _f(last.get("totalLiquidityUSD")) if isinstance(last, dict) else 0.0
        if len(series) >= 30 and isinstance(series[-30], dict):
            old = _f(series[-30].get("totalLiquidityUSD"))
            if old > 0:
                chg_30d = (tvl - old) / old * 100
    if tvl <= 0:
        # Fall back to the per-chain breakdown when the series is absent.
        chains = payload.get("currentChainTvls")
        if isinstance(chains, dict):
            tvl = sum(_f(v) for v in chains.values())
    if tvl <= 0:
        return None
    return {"tvl": tvl, "tvl_chg_30d": chg_30d}


# ── collector (network) ───────────────────────────────────────────────────
class ValuationCollector:
    """Fetches valuation snapshots for a universe and persists them once.

    One scheduled run covers every symbol; readers (the flow report, the signal
    context, the AI debate) take the persisted document instead of fetching
    again, so they cannot disagree about the same coin.
    """

    name = "onchain_valuation"

    def __init__(
        self,
        store,
        *,
        cg_base: str = "https://api.coingecko.com/api/v3",
        llama_base: str = "https://api.llama.fi",
        timeout: float = 15.0,
        cache_ttl: float = 3600.0,
        rate_limit_backoff: float = 900.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._store = store
        self._cg_base = cg_base.rstrip("/")
        self._llama_base = llama_base.rstrip("/")
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._backoff = rate_limit_backoff
        self._session = session or requests.Session()
        # Instance state — never module-level. Two collectors (or two tests) get
        # independent caches, and a collector can be thrown away to reset them.
        self._cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._ids: dict[str, Optional[str]] = {}
        self._rate_limited_until = 0.0

    # ── HTTP ──────────────────────────────────────────────────────────
    @property
    def rate_limited(self) -> bool:
        """True while a 429 backoff window is still open."""
        return time.time() < self._rate_limited_until

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[Any]:
        if self.rate_limited:
            log.debug("skipping %s — in 429 backoff", url)
            return None
        try:
            resp = self._session.get(
                url, params=params or {}, timeout=self._timeout,
                headers={"User-Agent": "wolf/1.0", "Accept": "application/json"},
            )
            # 429 is not a transient blip on a key-less public API — it means the
            # window is spent. Backing off beats retrying into a longer ban.
            if resp.status_code == 429:
                self._rate_limited_until = time.time() + self._backoff
                log.warning("Rate limited (429) on %s — pausing valuation fetches for %.0fs",
                            url, self._backoff)
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.debug("valuation HTTP error %s: %s", url, exc)
        except ValueError as exc:
            log.debug("valuation invalid JSON %s: %s", url, exc)
        return None

    def coin_id(self, symbol: str) -> Optional[str]:
        """Resolve a base symbol to a CoinGecko id, memoised on the instance.

        A symbol's id never changes, so the memo has no TTL — it exists to keep
        ``/search`` (the expensive path) to one call per symbol per process.
        """
        sym = base_symbol(symbol)
        if sym in CG_ID_OVERRIDE:
            return CG_ID_OVERRIDE[sym]
        if sym in self._ids:
            return self._ids[sym]
        data = self._get(f"{self._cg_base}/search", {"query": sym})
        resolved: Optional[str] = None
        coins = data.get("coins") if isinstance(data, dict) else None
        if isinstance(coins, list):
            for coin in coins:
                if isinstance(coin, dict) and str(coin.get("symbol", "")).upper() == sym:
                    resolved = coin.get("id")
                    break
            if resolved is None and coins and isinstance(coins[0], dict):
                resolved = coins[0].get("id")
        if data is not None:  # don't memoise a failure caused by backoff/network
            self._ids[sym] = resolved
        return resolved

    def fetch(self, symbol: str) -> Optional[dict]:
        """Fetch (or serve from cache) the raw payload bundle for one symbol."""
        sym = base_symbol(symbol)
        now = time.time()
        cached = self._cache.get(sym)
        if cached is not None and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        cid = self.coin_id(sym)
        raw = None
        if cid:
            raw = parse_markets_row(self._get(f"{self._cg_base}/coins/markets", {
                "vs_currency": "usd",
                "ids": cid,
                "price_change_percentage": "24h,7d,30d",
                "sparkline": "false",
            }))
        if raw is None:
            # Cache the miss too: without it an unlisted symbol is re-requested
            # every cycle, which is exactly how the rate limit gets spent.
            self._cache[sym] = (now, None)
            return None

        bundle = {"raw": raw, "tvl": self._fetch_tvl(sym)}
        self._cache[sym] = (now, bundle)
        return bundle

    def _fetch_tvl(self, base: str) -> Optional[dict]:
        slug = LLAMA_SLUG.get(base)
        if not slug:
            return None
        return parse_tvl(self._get(f"{self._llama_base}/protocol/{slug}"))

    # ── orchestration ─────────────────────────────────────────────────
    def valuation(self, symbol: str, direction: Optional[str] = None) -> Optional[dict]:
        """Full pipeline for one symbol: fetch → compute → assess → brief."""
        bundle = self.fetch(symbol)
        if not bundle:
            return None
        base = base_symbol(symbol)
        metrics = compute_valuation_metrics(bundle.get("raw"), bundle.get("tvl"))
        assessment = assess_valuation(metrics, direction)
        return {
            "symbol": base,
            "bias": assessment["bias"],
            "headline": assessment["headline"],
            "bull_notes": assessment["bull_notes"],
            "bear_notes": assessment["bear_notes"],
            "brief": build_valuation_brief(metrics, assessment, base),
            "metrics": metrics,
        }

    def collect(self, symbols: Iterable[str]) -> dict:
        """Refresh every symbol and persist one snapshot under ``STATE_KEY``.

        Persists whatever it got: a partial snapshot (some symbols rate-limited)
        is more useful than none, and every reader staleness-checks the timestamp
        anyway.
        """
        out: dict[str, dict] = {}
        for symbol in symbols:
            try:
                view = self.valuation(symbol)
            except (KeyError, ValueError, TypeError):
                log.warning("Valuation compute failed for %s", symbol, exc_info=True)
                continue
            if view is not None:
                out[view["symbol"]] = view

        doc = {
            "symbols": out,
            "ts": datetime.now(timezone.utc).isoformat(),
            "rate_limited": self.rate_limited,
        }
        self._store.write(STATE_KEY, doc)
        log.info("Valuation snapshot: %d symbol(s)%s",
                 len(out), " (rate limited)" if self.rate_limited else "")
        return doc


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _opt_f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
