"""Composite market regime — the macro backdrop for signal gating.

The single-axis :class:`~wolf.regime.RegimeProvider` only reads BTC trend. That
misses the market-wide *bounce/squeeze risk* that makes fresh SHORTs dangerous:
extreme fear, capital rotating back into risk assets, or USDT dominance sitting
at a wrung-out extreme. This module folds those free, key-less flow signals
(already fetched for the flow report) into one immutable :class:`MarketContext`
so the screener can *scale risk* on shorts — never a hard block, because the
direction out of extreme fear is genuinely uncertain (bounce OR continuation).

Design:
* **Fail-open.** Every dimension defaults to ``UNKNOWN`` and a failed fetch or a
  cold history leaves it ``UNKNOWN`` — which never scales anything. A data
  outage must not quietly shrink or filter every short.
* **Cheap.** BTC trend is read fresh each cycle; the slow-moving flow dimensions
  (F&G, USDT.D, stablecoin, chain) are cached with a TTL so the 10-minute
  screener loop doesn't hammer rate-limited public APIs.
* **Pure core.** Classification is pure functions over plain numbers; the
  provider only adds fetching, a TTL cache, and USDT.D history persistence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Optional

from wolf.regime import UNKNOWN

log = logging.getLogger("wolf.regime")

# ── dimension labels ────────────────────────────────────────────────────────
# Sentiment (Fear & Greed)
EXTREME_FEAR = "EXTREME_FEAR"
FEAR = "FEAR"
SENT_NEUTRAL = "SENT_NEUTRAL"
GREED = "GREED"
EXTREME_GREED = "EXTREME_GREED"

# Dry powder (stablecoin aggregate supply direction). Macro read: supply
# shrinking = redemptions = cash leaving crypto = risk-off.
DP_OUTFLOW = "DP_OUTFLOW"      # supply contracting → risk-off
DP_STABLE = "DP_STABLE"
DP_BUILDING = "DP_BUILDING"    # supply growing → sidelined cash accumulating

# Chain flow (aggregate on-chain activity / risk appetite)
CF_RISK_ON = "CF_RISK_ON"
CF_MIXED = "CF_MIXED"
CF_RISK_OFF = "CF_RISK_OFF"

# USDT dominance (ratio — cleaner than absolute stablecoin supply)
UD_RISK_OFF = "UD_RISK_OFF"            # USDT.D rising from a non-extreme level → short-friendly
UD_RISK_ON = "UD_RISK_ON"             # USDT.D falling → capital rotating into risk → short-reversal risk
UD_REVERSAL_RISK = "UD_REVERSAL_RISK"  # USDT.D at a historic extreme high → bounce risk
UD_NEUTRAL = "UD_NEUTRAL"

USDTD_HISTORY_KEY = "usdtd_history"


@dataclass(frozen=True)
class MarketContext:
    """Immutable snapshot of the macro backdrop, consumed by the screener.

    Raw values (``fng_value`` etc.) are carried so the monitor-mode what-if log
    can explain *why* a short was flagged without re-fetching anything.
    """

    trend: str = UNKNOWN
    sentiment: str = UNKNOWN
    dry_powder: str = UNKNOWN
    chain_flow: str = UNKNOWN
    usdt_d: str = UNKNOWN

    fng_value: Optional[int] = None
    usdtd_value: Optional[float] = None
    usdtd_change_24h: Optional[float] = None
    usdtd_percentile: Optional[float] = None

    @property
    def short_reversal_risk(self) -> bool:
        """True when a fresh SHORT faces elevated bounce/squeeze risk.

        Extreme fear (capitulation can snap back hard) or USDT.D signalling a
        rotation back into risk / a wrung-out extreme. Drives *risk scaling*,
        not a block — including for counter-trend setups (PREDUMP/TRAP/SCALP),
        which is exactly the blind spot the trend-only regime filter misses.
        """
        return (
            self.sentiment == EXTREME_FEAR
            or self.usdt_d in (UD_RISK_ON, UD_REVERSAL_RISK)
        )

    @property
    def short_risk_off(self) -> bool:
        """True when the macro backdrop is short-friendly (no penalty)."""
        return self.usdt_d == UD_RISK_OFF or self.dry_powder == DP_OUTFLOW


# ── pure classification helpers ─────────────────────────────────────────────
def classify_sentiment(fng_value: Optional[int], extreme_fear_max: int = 25) -> str:
    if fng_value is None:
        return UNKNOWN
    if fng_value <= extreme_fear_max:
        return EXTREME_FEAR
    if fng_value <= 45:
        return FEAR
    if fng_value >= 75:
        return EXTREME_GREED
    if fng_value >= 60:
        return GREED
    return SENT_NEUTRAL


def classify_dry_powder(change_1d_pct: Optional[float], outflow_pct: float = -0.5) -> str:
    if change_1d_pct is None:
        return UNKNOWN
    if change_1d_pct <= outflow_pct:
        return DP_OUTFLOW
    if change_1d_pct >= abs(outflow_pct):
        return DP_BUILDING
    return DP_STABLE


def classify_chain_flow(
    mcap_change_24h: Optional[float], top_chain_change_1d: Optional[float]
) -> str:
    if mcap_change_24h is None and top_chain_change_1d is None:
        return UNKNOWN
    mcap_up = (mcap_change_24h or 0.0) > 0
    chain_up = (top_chain_change_1d or 0.0) > 0
    if mcap_up and chain_up:
        return CF_RISK_ON
    if not mcap_up and not chain_up:
        return CF_RISK_OFF
    return CF_MIXED


def pct_change_24h(
    history: list[dict], current: float, now_ts: float, tolerance_h: float = 6.0
) -> Optional[float]:
    """Percent change of ``current`` vs the recorded value nearest 24h ago.

    ``history`` is a list of ``{"ts": epoch_seconds, "value": pct}``. Returns
    ``None`` when no sample sits within ``tolerance_h`` of the 24h mark.
    """
    target = now_ts - 24 * 3600
    best: Optional[dict] = None
    best_gap = tolerance_h * 3600
    for row in history:
        ts = row.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        gap = abs(ts - target)
        if gap <= best_gap:
            best_gap = gap
            best = row
    if best is None or not best.get("value"):
        return None
    prev = float(best["value"])
    if prev == 0:
        return None
    return (current - prev) / prev * 100


def percentile_rank(values: list[float], current: float) -> Optional[float]:
    """Percentile (0–100) of ``current`` within ``values`` (inclusive rank)."""
    if not values:
        return None
    below = sum(1 for v in values if v <= current)
    return below / len(values) * 100


def classify_usdt_d(
    change_24h: Optional[float],
    percentile: Optional[float],
    change_threshold_pct: float = 0.2,
    reversal_percentile: float = 85.0,
) -> str:
    """Classify USDT.D. Reversal-risk (historic extreme) takes precedence.

    * extreme-high percentile → ``UD_REVERSAL_RISK`` (bounce risk, scale shorts)
    * rising ≥ threshold from a non-extreme level → ``UD_RISK_OFF`` (short-friendly)
    * falling ≤ −threshold → ``UD_RISK_ON`` (rotation into risk, short-reversal risk)
    """
    if percentile is not None and percentile > reversal_percentile:
        return UD_REVERSAL_RISK
    if change_24h is None:
        return UNKNOWN
    if change_24h >= change_threshold_pct:
        return UD_RISK_OFF
    if change_24h <= -change_threshold_pct:
        return UD_RISK_ON
    return UD_NEUTRAL


class CompositeRegimeProvider:
    """Compose BTC trend + flow dimensions into a cached :class:`MarketContext`.

    Reuses the existing flow clients; adds only a TTL cache and USDT.D history
    persistence. Any fetch failure degrades the affected dimension to
    ``UNKNOWN`` (fail-open).
    """

    def __init__(
        self,
        trend_provider,
        sentiment_client=None,
        coingecko_client=None,
        defillama_client=None,
        store=None,
        *,
        fear_extreme_max: int = 25,
        usdtd_change_pct: float = 0.2,
        usdtd_reversal_percentile: float = 85.0,
        usdtd_history_days: int = 90,
        usdtd_min_history_days: int = 7,
        dry_powder_outflow_pct: float = -0.5,
        ttl_min: int = 30,
        clock=time.time,
    ) -> None:
        self._trend = trend_provider
        self._sentiment = sentiment_client
        self._coingecko = coingecko_client
        self._defillama = defillama_client
        self._store = store
        self._fear_extreme_max = fear_extreme_max
        self._usdtd_change_pct = usdtd_change_pct
        self._usdtd_reversal_pct = usdtd_reversal_percentile
        self._usdtd_history_days = usdtd_history_days
        self._usdtd_min_history_days = usdtd_min_history_days
        self._dry_powder_outflow_pct = dry_powder_outflow_pct
        self._ttl_s = max(0, ttl_min) * 60
        self._clock = clock
        self._cached_flow: Optional[MarketContext] = None
        self._cached_at: float = 0.0

    # ── public API ──────────────────────────────────────────────────────
    def snapshot(self) -> MarketContext:
        """Return the current context: fresh trend + TTL-cached flow dims."""
        trend = self._safe_trend()
        flow = self._flow_dims()
        return replace(flow, trend=trend)

    # ── trend (cheap, fetched every cycle) ──────────────────────────────
    def _safe_trend(self) -> str:
        if self._trend is None:
            return UNKNOWN
        try:
            return self._trend.bias()
        except Exception:  # a regime hiccup must never break the scan
            log.warning("Composite: trend fetch failed", exc_info=True)
            return UNKNOWN

    # ── flow dimensions (TTL cached) ────────────────────────────────────
    def _flow_dims(self) -> MarketContext:
        now = self._clock()
        if self._cached_flow is not None and (now - self._cached_at) < self._ttl_s:
            return self._cached_flow
        ctx = self._build_flow_dims(now)
        self._cached_flow = ctx
        self._cached_at = now
        return ctx

    def _build_flow_dims(self, now: float) -> MarketContext:
        g = self._safe_global()  # one /global fetch shared by usdt_d + chain_flow
        sentiment, fng_value = self._sentiment_dim()
        dry_powder = self._dry_powder_dim()
        chain_flow = self._chain_flow_dim(g)
        usdt_d, ud_val, ud_change, ud_pct = self._usdt_d_dim(g, now)
        return MarketContext(
            trend=UNKNOWN,  # filled fresh in snapshot()
            sentiment=sentiment,
            dry_powder=dry_powder,
            chain_flow=chain_flow,
            usdt_d=usdt_d,
            fng_value=fng_value,
            usdtd_value=ud_val,
            usdtd_change_24h=ud_change,
            usdtd_percentile=ud_pct,
        )

    def _sentiment_dim(self) -> tuple[str, Optional[int]]:
        if self._sentiment is None:
            return UNKNOWN, None
        try:
            fg = self._sentiment.fear_greed()
        except Exception:
            log.debug("Composite: fear_greed fetch failed", exc_info=True)
            return UNKNOWN, None
        if fg is None:
            return UNKNOWN, None
        return classify_sentiment(fg.value, self._fear_extreme_max), fg.value

    def _dry_powder_dim(self) -> str:
        if self._defillama is None:
            return UNKNOWN
        try:
            s = self._defillama.stablecoin_supply()
        except Exception:
            log.debug("Composite: stablecoin fetch failed", exc_info=True)
            return UNKNOWN
        if s is None:
            return UNKNOWN
        return classify_dry_powder(s.change_1d_pct, self._dry_powder_outflow_pct)

    def _safe_global(self):
        if self._coingecko is None:
            return None
        try:
            return self._coingecko.global_data()
        except Exception:
            log.debug("Composite: global fetch failed", exc_info=True)
            return None

    def _chain_flow_dim(self, g) -> str:
        if g is None and self._defillama is None:
            return UNKNOWN
        mcap_change: Optional[float] = g.market_cap_change_24h if g is not None else None
        top_chain_change: Optional[float] = None
        try:
            if self._defillama is not None:
                chains = self._defillama.chain_activity()
                if chains:
                    top_chain_change = max(c.change_1d for c in chains)
        except Exception:
            log.debug("Composite: chain fetch failed", exc_info=True)
        if mcap_change is None and top_chain_change is None:
            return UNKNOWN
        return classify_chain_flow(mcap_change, top_chain_change)

    def _usdt_d_dim(
        self, g, now: float
    ) -> tuple[str, Optional[float], Optional[float], Optional[float]]:
        if g is None or not g.usdt_dominance:
            return UNKNOWN, None, None, None
        value = float(g.usdt_dominance)
        history = self._load_usdtd_history()
        change_24h = pct_change_24h(history, value, now)
        # Percentile only once we have enough history to be meaningful.
        min_pts_span = now - self._usdtd_min_history_days * 24 * 3600
        oldest_ts = min((r.get("ts", now) for r in history), default=now)
        percentile: Optional[float] = None
        if history and oldest_ts <= min_pts_span:
            percentile = percentile_rank([float(r["value"]) for r in history if r.get("value")], value)
        self._record_usdtd(history, value, now)
        label = classify_usdt_d(
            change_24h, percentile, self._usdtd_change_pct, self._usdtd_reversal_pct
        )
        return label, value, change_24h, percentile

    # ── USDT.D history persistence ──────────────────────────────────────
    def _load_usdtd_history(self) -> list[dict]:
        if self._store is None:
            return []
        raw = self._store.read(USDTD_HISTORY_KEY, default=[])
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []

    def _record_usdtd(self, history: list[dict], value: float, now: float) -> None:
        if self._store is None:
            return
        cutoff = now - self._usdtd_history_days * 24 * 3600
        pruned = [r for r in history if isinstance(r.get("ts"), (int, float)) and r["ts"] >= cutoff]
        pruned.append({"ts": now, "value": round(value, 4)})
        self._store.write(USDTD_HISTORY_KEY, pruned)
