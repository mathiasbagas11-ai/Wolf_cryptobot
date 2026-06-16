"""Flow brief — turn raw metrics into the structured intelligence the report needs.

This is the deterministic core that encodes the *framework filter* from the
source account (FDV/MC unlock pressure, liquidity/turnover, wash-trading and
already-pumped FOMO guards) and classifies tokens into PICKS vs SKIPS with a
human-readable reason for each. It is a pure function of the fetched metrics so
it unit-tests with canned data — the LLM narrator (or the template fallback)
only ever *renders* this brief, never invents the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from wolf.flow.coingecko import GlobalMetrics, TokenMetrics
from wolf.flow.defillama import ChainActivity, StablecoinSupply
from wolf.flow.sentiment import CoinbasePremium, FearGreed

# ── framework thresholds (mirror the source account's filter) ──────────────
FDV_MC_MAX = 2.0          # < 2x → low outstanding unlock pressure (good)
VOL_MC_MIN = 0.10         # > 10% mcap turnover → liquid enough to exit
VOL_MC_WASH = 3.0         # > 3x mcap turnover → likely wash / artificial volume
PUMP_MAX = 25.0           # already +25% in 24h → FOMO trap, skip
PULLBACK_MIN = -12.0      # deeper than -12% in 24h → not a healthy pullback
PICK_MCAP_MAX = 750_000_000   # "runway besar" — favour smaller caps for picks
PICK_MCAP_MIN = 3_000_000     # ignore dust / illiquid micro caps

#: Excluded from PICKS — stablecoins & wrapped/pegged tokens are not alpha plays.
NON_ALPHA = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD",
    "WETH", "WBTC", "WBETH", "STETH", "WSTETH", "WEETH", "CBBTC", "TBTC",
    "BTC", "ETH",  # majors get their own BTC FLOW section
}


@dataclass
class Pick:
    symbol: str
    name: str
    price: float
    change_24h: float
    market_cap: float
    fdv_mc: Optional[float]
    vol_mc: Optional[float]
    ath_change_pct: float = 0.0
    liquidity_pctile: float = 0.0          # turnover rank within the scanned universe
    funding_rate: Optional[float] = None   # percent; enriched post-build by the reporter
    reasons: list[str] = field(default_factory=list)

    @property
    def funding_signal(self) -> Optional[str]:
        return funding_signal(self.funding_rate)

    @property
    def quant_score(self) -> int:
        """0–100 composite: low unlock + healthy turnover + funding tailwind."""
        score = 0.0
        if self.fdv_mc is not None:
            # FDV/MC 1.0 (fully circulating, no unlocks) → full credit; ≥2.0 → none.
            unlock = max(0.0, min(1.0, (FDV_MC_MAX - self.fdv_mc) / (FDV_MC_MAX - 1.0)))
            score += unlock * 40                                          # ≤40
        score += min(self.liquidity_pctile, 100.0) / 100.0 * 35           # ≤35
        sig = self.funding_signal
        if sig == "BULLISH":
            score += 25
        elif sig == "NEUTRAL":
            score += 12
        return int(round(min(score, 100.0)))

    @property
    def entry_note(self) -> str:
        if self.change_24h <= 0:
            return "entry zone: sekarang (pullback sehat)"
        if self.change_24h >= 12:
            return "udah naik — tunggu pullback sebelum entry"
        return "entry zone: sekarang"


@dataclass
class Skip:
    symbol: str
    reason: str


@dataclass
class FlowBrief:
    btc: Optional[GlobalMetrics] = None
    fear_greed: Optional[FearGreed] = None
    coinbase_premium: Optional[CoinbasePremium] = None
    stablecoin: Optional[StablecoinSupply] = None
    chains: list[ChainActivity] = field(default_factory=list)
    picks: list[Pick] = field(default_factory=list)
    watchlist: list[Pick] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    conclusion: str = ""
    stance: str = "NEUTRAL"   # RISK-ON | RISK-OFF | ROTATION | NEUTRAL

    @property
    def has_content(self) -> bool:
        return bool(self.picks or self.chains or self.btc or self.stablecoin)


def funding_signal(rate: Optional[float]) -> Optional[str]:
    """Map a funding rate (percent) to a directional read.

    Negative funding → shorts pay longs → shorts crowded → squeeze fuel (BULLISH).
    High positive funding → longs overheated (BEARISH). Mirrors ``wolf.market``.
    """
    if rate is None:
        return None
    if rate < -0.01:
        return "BULLISH"
    if rate > 0.05:
        return "BEARISH"
    return "NEUTRAL"


def build_brief(
    markets: list[TokenMetrics],
    global_metrics: Optional[GlobalMetrics],
    chains: list[ChainActivity],
    stablecoin: Optional[StablecoinSupply],
    *,
    fear_greed: Optional[FearGreed] = None,
    coinbase_premium: Optional[CoinbasePremium] = None,
    max_picks: int = 3,
    max_skips: int = 4,
    max_watch: int = 2,
) -> FlowBrief:
    candidates: list[tuple[float, Pick]] = []
    skips: list[Skip] = []

    # Liquidity percentile is cross-sectional: rank each token's turnover against
    # the whole scanned, tradable universe (excluding stables/wrapped).
    universe_vol_mc = sorted(
        t.vol_mc for t in markets if t.symbol not in NON_ALPHA and t.vol_mc is not None
    )

    for t in markets:
        if t.symbol in NON_ALPHA:
            continue
        fdv_mc, vol_mc = t.fdv_mc, t.vol_mc

        # ── SKIP rules (FOMO / wash / unlock pressure) ──
        if t.change_24h > PUMP_MAX:
            skips.append(Skip(t.symbol, f"udah pump +{t.change_24h:.1f}% hari ini — FOMO trap"))
            continue
        if vol_mc is not None and vol_mc > VOL_MC_WASH:
            skips.append(Skip(t.symbol, f"volume {vol_mc:.1f}x mcap — sinyal wash/artificial"))
            continue
        if fdv_mc is not None and fdv_mc >= FDV_MC_MAX:
            skips.append(Skip(t.symbol, f"FDV/MC {fdv_mc:.1f}x — tekanan unlock besar"))
            continue

        # ── PICK gating ──
        if not (PICK_MCAP_MIN <= t.market_cap <= PICK_MCAP_MAX):
            continue
        if t.change_24h < PULLBACK_MIN:
            continue
        if vol_mc is None or vol_mc < VOL_MC_MIN:
            continue
        if fdv_mc is None:
            continue

        pctile = _percentile(universe_vol_mc, vol_mc)
        reasons = [f"FDV/MC {fdv_mc:.1f}x = tekanan unlock minim"]
        reasons.append(f"likuiditas {pctile:.0f} percentile (turnover {vol_mc * 100:.0f}% mcap)")
        reasons.append("mcap kecil = runway masih besar")
        if t.ath_change_pct <= -70:
            reasons.append(f"{t.ath_change_pct:.0f}% dari ATH = downside udah banyak ke-flush")
        if t.change_24h < 0:
            reasons.append(f"pullback {t.change_24h:.1f}% = timing entry lebih baik")
        pick = Pick(
            symbol=t.symbol, name=t.name, price=t.price, change_24h=t.change_24h,
            market_cap=t.market_cap, fdv_mc=fdv_mc, vol_mc=vol_mc,
            ath_change_pct=t.ath_change_pct, liquidity_pctile=pctile, reasons=reasons,
        )
        # Rank by the funding-agnostic part of the quant score (funding is enriched
        # later, only for the displayed set, to bound API calls).
        candidates.append((pick.quant_score, pick))

    candidates.sort(key=lambda c: c[0], reverse=True)
    ranked = [p for _, p in candidates]
    picks = ranked[:max_picks]
    watchlist = ranked[max_picks:max_picks + max_watch]

    chains_sorted = sorted(chains, key=lambda c: c.change_1d, reverse=True)
    stance, conclusion = _stance(global_metrics, stablecoin, chains_sorted,
                                 fear_greed, coinbase_premium)

    return FlowBrief(
        btc=global_metrics,
        fear_greed=fear_greed,
        coinbase_premium=coinbase_premium,
        stablecoin=stablecoin,
        chains=chains_sorted,
        picks=picks,
        watchlist=watchlist,
        skips=skips[:max_skips],
        conclusion=conclusion,
        stance=stance,
    )


def _percentile(sorted_values: list[float], value: float) -> float:
    """Percentile rank (0–100) of ``value`` within ``sorted_values``."""
    n = len(sorted_values)
    if n <= 1:
        return 100.0
    below = sum(1 for v in sorted_values if v < value)
    return below / (n - 1) * 100.0


def _stance(g: Optional[GlobalMetrics], s: Optional[StablecoinSupply],
            chains: list[ChainActivity], fg: Optional[FearGreed] = None,
            cb: Optional[CoinbasePremium] = None) -> tuple[str, str]:
    """Heuristic market posture from dominance, dry-powder, chain activity and
    the contrarian fear/institutional-demand pair."""
    dry_powder = s is not None and s.change_7d_pct > 0.5
    chain_heat = bool(chains) and chains[0].change_1d > 0
    alts_bid = g is not None and g.market_cap_change_24h > 0 and g.btc_dominance < 56

    # Contrarian read: crowd fearful + US institutions bidding + dry powder ready.
    if fg is not None and fg.is_fear and cb is not None and cb.signal == "ACCUMULATION" and dry_powder:
        return ("RISK-ON (contrarian)",
                f"Fear & Greed {fg.value} ({fg.classification}) di permukaan, tapi "
                f"Coinbase premium +{cb.premium_pct:.2f}% = institusi US lagi akumulasi + "
                "dry powder numpuk. 'Be greedy when others are fearful.'")

    if dry_powder and chain_heat and alts_bid:
        top = chains[0].label if chains else "altcoin"
        return ("RISK-ON", f"Dry powder numpuk + aktivitas rotasi ke {top}. "
                           "Smart money positioning buat naik — bukan kabur.")
    if dry_powder and not alts_bid:
        return ("ROTATION", "Stablecoin numpuk jadi amunisi, tapi modal belum agresif "
                            "masuk altcoin — tunggu konfirmasi rotasi.")
    if g is not None and g.market_cap_change_24h < -2:
        return ("RISK-OFF", "Market cap turun & dry powder belum dilepas — hati-hati, "
                            "tunggu smart money mulai deploy.")
    return ("NEUTRAL", "Sinyal campur — belum ada arah modal yang jelas. "
                       "Pantau dry powder & rotasi chain sebelum eksekusi.")
