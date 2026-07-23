"""Phase 2 — OHLC fetcher.

Pulls ~90 days of daily candles per coin so the scoring engine can measure
range compression and volume anomalies. CoinGecko splits the data across two
endpoints — ``/coins/{id}/ohlc`` (no volume) and ``/coins/{id}/market_chart``
(volume series) — so we fetch both and merge volume onto each candle by nearest
timestamp.

The free API is tightly rate-limited (~10-30 calls/min), so every call is
spaced by ``time.sleep(2.5)`` and retried with exponential backoff; a 429 backs
off a full 60s. Each coin's merged frame is cached to
``data/ohlc/{coin_id}.parquet`` (TTL 4h) so a re-scan within the window is free.

The merge (:func:`merge_ohlc_volume`) is a pure DataFrame transform, unit-tested
without the network.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger("wolf.anomaly")

_CG_BASE = "https://api.coingecko.com/api/v3"
_USER_AGENT = "wolf/1.0"
_MAX_RETRIES = 3
_CALL_SPACING_SEC = 2.5          # min gap between CoinGecko calls
_RATE_LIMIT_SLEEP_SEC = 60       # backoff when a 429 is seen

_CACHE_DIR = os.path.join("data", "ohlc")
_CACHE_TTL_SEC = 4 * 60 * 60     # 4h

_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def fetch_ohlc(
    coin_id: str,
    days: int = 90,
    *,
    base_url: str = _CG_BASE,
    session: Optional[requests.Session] = None,
    cache_dir: str = _CACHE_DIR,
    use_cache: bool = True,
    sleep: float = _CALL_SPACING_SEC,
) -> pd.DataFrame:
    """Return a merged OHLCV DataFrame for ``coin_id``.

    Columns: ``timestamp, open, high, low, close, volume`` (``timestamp`` is
    tz-aware UTC). Reads the 4h parquet cache when fresh; otherwise fetches the
    OHLC and volume series, merges them, caches and returns. Returns an empty
    frame (correct columns) if the data can't be fetched.
    """
    if use_cache:
        cached = _read_fresh_cache(cache_dir, coin_id)
        if cached is not None:
            log.debug("ohlc %s: %d rows from fresh cache", coin_id, len(cached))
            return cached

    sess = session or requests.Session()

    ohlc_raw = _get_with_retry(sess, f"{base_url}/coins/{coin_id}/ohlc", {
        "vs_currency": "usd", "days": days,
    }, sleep=sleep)
    vol_raw = _get_with_retry(sess, f"{base_url}/coins/{coin_id}/market_chart", {
        "vs_currency": "usd", "days": days,
    }, sleep=sleep)

    df = merge_ohlc_volume(ohlc_raw, vol_raw)
    if not df.empty and use_cache:
        _write_cache(cache_dir, coin_id, df)
    return df


# ── pure merge (testable without the network) ──────────────────────────────
def merge_ohlc_volume(ohlc_raw, market_chart_raw) -> pd.DataFrame:
    """Build the OHLCV frame from the two raw CoinGecko payloads.

    ``ohlc_raw`` is a list of ``[ms, open, high, low, close]`` rows.
    ``market_chart_raw`` is a dict with ``total_volumes`` = ``[[ms, volume], …]``.
    Volume is joined onto each candle by nearest timestamp (the two series use
    different cadences), and NaN volumes fall back to 0.
    """
    if not isinstance(ohlc_raw, list) or not ohlc_raw:
        return _empty()

    rows = [r for r in ohlc_raw if isinstance(r, (list, tuple)) and len(r) >= 5]
    if not rows:
        return _empty()

    ohlc = pd.DataFrame(rows, columns=["ms", "open", "high", "low", "close"])
    ohlc["timestamp"] = pd.to_datetime(ohlc["ms"], unit="ms", utc=True)
    ohlc = ohlc.drop(columns="ms").sort_values("timestamp").reset_index(drop=True)

    volumes = None
    if isinstance(market_chart_raw, dict):
        volumes = market_chart_raw.get("total_volumes")

    if isinstance(volumes, list) and volumes:
        vol_rows = [v for v in volumes if isinstance(v, (list, tuple)) and len(v) >= 2]
    else:
        vol_rows = []

    if vol_rows:
        vol = pd.DataFrame(vol_rows, columns=["ms", "volume"])
        vol["timestamp"] = pd.to_datetime(vol["ms"], unit="ms", utc=True)
        vol = vol.drop(columns="ms").sort_values("timestamp").reset_index(drop=True)
        # Nearest-timestamp join: the OHLC and volume series don't share stamps.
        merged = pd.merge_asof(
            ohlc, vol, on="timestamp", direction="nearest",
        )
    else:
        merged = ohlc.copy()
        merged["volume"] = 0.0

    merged["volume"] = merged["volume"].fillna(0.0)
    for col in ("open", "high", "low", "close"):
        merged[col] = pd.to_numeric(merged[col], errors="coerce")
    return merged[_COLUMNS].reset_index(drop=True)


def fetch_prices(
    coin_ids: list[str],
    *,
    base_url: str = _CG_BASE,
    session: Optional[requests.Session] = None,
    sleep: float = _CALL_SPACING_SEC,
    chunk_size: int = 100,
) -> dict:
    """Return ``{coin_id: usd_price}`` from CoinGecko ``/simple/price``.

    Batched (comma-separated ids) to spend as few calls as possible — used by
    the daily outcome-backfill job, which needs a spot price per open signal.
    """
    sess = session or requests.Session()
    out: dict[str, float] = {}
    ids = [c for c in dict.fromkeys(coin_ids) if c]      # dedupe, keep order
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        payload = _get_with_retry(sess, f"{base_url}/simple/price", {
            "ids": ",".join(chunk), "vs_currencies": "usd",
        }, sleep=sleep)
        if isinstance(payload, dict):
            for cid, d in payload.items():
                if isinstance(d, dict) and d.get("usd") is not None:
                    try:
                        out[cid] = float(d["usd"])
                    except (TypeError, ValueError):
                        continue
    return out


# ── network fetch ──────────────────────────────────────────────────────────
def _get_with_retry(session: requests.Session, url: str, params: dict, *, sleep: float):
    """Rate-limit-friendly GET: space calls, back off on error, 60s on a 429."""
    for attempt in range(_MAX_RETRIES):
        if sleep:
            time.sleep(sleep)           # WAJIB: space every CoinGecko call
        try:
            resp = session.get(url, params=params, timeout=20,
                               headers={"User-Agent": _USER_AGENT})
            if resp.status_code == 429:
                log.warning("ohlc: 429 rate-limited on %s — sleeping %ds", url, _RATE_LIMIT_SLEEP_SEC)
                time.sleep(_RATE_LIMIT_SLEEP_SEC)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            wait = 2 ** attempt
            log.debug("ohlc: fetch %s failed (%s); retry in %ds", url, exc, wait)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(wait)
    return None


# ── cache (parquet, per coin) ──────────────────────────────────────────────
def _cache_path(cache_dir: str, coin_id: str) -> str:
    safe = "".join(c for c in coin_id if c.isalnum() or c in ("-", "_")) or "coin"
    return os.path.join(cache_dir, f"{safe}.parquet")


def _read_fresh_cache(cache_dir: str, coin_id: str) -> Optional[pd.DataFrame]:
    path = _cache_path(cache_dir, coin_id)
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > _CACHE_TTL_SEC:
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:                     # noqa: BLE001 — corrupt cache is non-fatal
        log.warning("ohlc: cache unreadable for %s (%s)", coin_id, exc)
        return None


def _write_cache(cache_dir: str, coin_id: str, df: pd.DataFrame) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, coin_id)
    tmp = f"{path}.tmp"
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)                    # atomic swap
    except Exception as exc:                     # noqa: BLE001 — caching must never break a scan
        log.error("ohlc: failed to cache %s (%s)", coin_id, exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in _COLUMNS})
