"""Phase 1 — Universe filter.

Builds the tradable universe the anomaly scanner scans: mid-cap coins with
enough liquidity to enter/exit and a long-enough listing history to have formed
a real range. The heavy filtering here keeps the OHLC-fetch phase (rate-limit
bound) pointed only at coins worth scanning.

Data source: CoinGecko ``/coins/markets`` (free, key-less), pages 1 & 2 ordered
by 24h volume. The pure filter (:func:`filter_universe`) is separated from the
network fetch so it unit-tests against canned payloads without hitting the API —
important here because the live endpoint is rate-limited and often unreachable
from CI.

The result is cached to ``data/universe.json`` (TTL 24h) so repeated scans in a
day don't burn the free rate limit.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("wolf.anomaly")

# ── filter thresholds ──────────────────────────────────────────────────────
MCAP_MIN = 20_000_000            # below → too small / illiquid micro cap
MCAP_MAX = 2_000_000_000         # above → too big, little swing runway left
VOLUME_MIN = 2_000_000           # 24h volume floor → can actually get filled
MIN_LISTING_AGE_DAYS = 30        # younger than this → no range history yet

#: Pegged / wrapped assets are never swing candidates — drop outright.
STABLECOIN_BLACKLIST = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USDD",
    "GUSD", "USDP", "FRAX", "LUSD", "SUSD", "USDG", "BUIDL", "USD1", "RLUSD",
    "EURC", "EURT", "USDL",
}

#: Coins the user already DCAs into. Kept in the universe but flagged so the
#: scanner can size/treat them differently — never silently dropped.
DCA_HOLDINGS = ["BTC", "SOL", "TAO", "AERO"]

# ── CoinGecko fetch config ─────────────────────────────────────────────────
_CG_BASE = "https://api.coingecko.com/api/v3"
_USER_AGENT = "wolf/1.0"
_PER_PAGE = 250
_PAGES = (1, 2)
_MAX_RETRIES = 3

# ── cache config ───────────────────────────────────────────────────────────
_CACHE_PATH = os.path.join("data", "universe.json")
_CACHE_TTL_SEC = 24 * 60 * 60    # 24h


def build_universe(
    *,
    base_url: str = _CG_BASE,
    session: Optional[requests.Session] = None,
    cache_path: str = _CACHE_PATH,
    use_cache: bool = True,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Return the filtered scan universe as a list of coin dicts.

    Reads from the 24h cache when fresh; otherwise fetches ``/coins/markets``
    pages 1 & 2, applies :func:`filter_universe`, caches the result and returns
    it. On a fetch failure a stale cache (if any) is returned rather than an
    empty list, so a transient CoinGecko outage doesn't blank the scanner.
    """
    now = now or datetime.now(timezone.utc)

    if use_cache:
        cached = _read_fresh_cache(cache_path)
        if cached is not None:
            log.debug("universe: %d coins from fresh cache", len(cached))
            return cached

    raw = _fetch_markets(base_url=base_url, session=session)
    if not raw:
        stale = _read_cache(cache_path)
        if stale is not None:
            log.warning("universe: fetch failed — serving stale cache (%d coins)", len(stale.get("coins", [])))
            return stale.get("coins", [])
        return []

    coins = filter_universe(raw, now=now)
    if use_cache:
        _write_cache(cache_path, coins, now)
    return coins


# ── pure filtering (testable without the network) ──────────────────────────
def filter_universe(rows: list[dict], *, now: Optional[datetime] = None) -> list[dict]:
    """Apply the universe filters to raw ``/coins/markets`` rows.

    Drops stablecoins, coins outside the mcap band, thin-volume coins and
    freshly-listed coins (ATL date < 30d as a listing-age proxy). DCA holdings
    are kept and flagged ``in_dca_sleeve=True`` instead of being dropped.
    """
    now = now or datetime.now(timezone.utc)
    dca = {s.upper() for s in DCA_HOLDINGS}
    out: list[dict] = []
    seen: set[str] = set()

    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = str(r.get("symbol", "")).upper()
        coin_id = str(r.get("id", ""))
        if not coin_id or coin_id in seen:
            continue

        is_dca = symbol in dca

        # DCA holdings bypass every drop rule but the blacklist below.
        if not is_dca:
            if symbol in STABLECOIN_BLACKLIST:
                continue
            mcap = _f(r.get("market_cap"))
            if mcap < MCAP_MIN or mcap > MCAP_MAX:
                continue
            if _f(r.get("total_volume")) < VOLUME_MIN:
                continue
            if _too_new(r.get("atl_date"), now):
                continue
        elif symbol in STABLECOIN_BLACKLIST:
            # A DCA symbol that is somehow a stablecoin is still not a swing coin.
            continue

        seen.add(coin_id)
        out.append({
            "id": coin_id,
            "symbol": symbol,
            "name": str(r.get("name", "")),
            "current_price": _f(r.get("current_price")),
            "market_cap": _f(r.get("market_cap")),
            "total_volume": _f(r.get("total_volume")),
            "fdv": _f(r.get("fully_diluted_valuation")),
            "price_change_percentage_24h": _f(r.get("price_change_percentage_24h")),
            "in_dca_sleeve": is_dca,
        })
    return out


def _too_new(atl_date, now: datetime) -> bool:
    """True if the coin's ATL date is < MIN_LISTING_AGE_DAYS ago (too new).

    ATL date is a proxy for listing age. A null/unparseable date means we can't
    tell → skip the age check (keep the coin) rather than drop it.
    """
    if not atl_date:
        return False
    dt = _parse_iso(atl_date)
    if dt is None:
        return False
    age_days = (now - dt).total_seconds() / 86_400
    return age_days < MIN_LISTING_AGE_DAYS


# ── network fetch ──────────────────────────────────────────────────────────
def _fetch_markets(*, base_url: str, session: Optional[requests.Session]) -> list[dict]:
    sess = session or requests.Session()
    rows: list[dict] = []
    for page in _PAGES:
        payload = _get_with_retry(sess, f"{base_url}/coins/markets", {
            "vs_currency": "usd",
            "order": "volume_desc",
            "per_page": _PER_PAGE,
            "page": page,
            "price_change_percentage": "24h",
        })
        if isinstance(payload, list):
            rows.extend(payload)
        else:
            log.warning("universe: page %d fetch returned no data", page)
    return rows


def _get_with_retry(session: requests.Session, url: str, params: dict):
    """GET with exponential backoff; long sleep on a 429 rate-limit."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = session.get(url, params=params, timeout=20,
                               headers={"User-Agent": _USER_AGENT})
            if resp.status_code == 429:
                log.warning("universe: 429 rate-limited — sleeping 60s")
                time.sleep(60)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            wait = 2 ** attempt
            log.debug("universe: fetch %s failed (%s); retry in %ds", url, exc, wait)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(wait)
    return None


# ── cache ──────────────────────────────────────────────────────────────────
def _read_cache(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("coins"), list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("universe: cache unreadable (%s)", exc)
    return None


def _read_fresh_cache(path: str) -> Optional[list[dict]]:
    data = _read_cache(path)
    if data is None:
        return None
    cached_at = _parse_iso(data.get("cached_at"))
    if cached_at is None:
        return None
    age = (datetime.now(timezone.utc) - cached_at).total_seconds()
    if age > _CACHE_TTL_SEC:
        return None
    return data.get("coins", [])


def _write_cache(path: str, coins: list[dict], now: datetime) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    payload = {"cached_at": now.astimezone(timezone.utc).isoformat(), "coins": coins}
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic swap
    except OSError as exc:
        log.error("universe: failed to write cache: %s", exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ── helpers ────────────────────────────────────────────────────────────────
def _parse_iso(value) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (CoinGecko uses trailing 'Z') to aware UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _f(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0
