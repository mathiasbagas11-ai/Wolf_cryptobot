"""Token deep-dive analysis — bull vs bear factors from a token's metrics.

The deterministic core behind :class:`~wolf.reports.deepdive.TokenDeepDive`: a
pure function of already-fetched metrics, so it unit-tests with canned data and
the LLM narrator only ever *renders* what it produced.

This module used to also hold ``build_brief`` — the pick/skip screen behind the
old flow digest. That digest was replaced by :mod:`wolf.reports.flow`, which
reads collector snapshots and screens with filters that do not mistake a
stablecoin for a find. The old screen is gone rather than left exported,
because its ``Pick.entry_note`` derived an "entry zone" from nothing but the 24h
price change: a price recommendation with no price analysis behind it, and
exactly the thing a future caller should not be able to pick back up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from wolf.flow.coingecko import TokenMetrics

#: Below this market cap a token is small enough that the playbook argues for
#: sizing down and averaging in rather than taking a full position.
SMALL_CAP_MAX = 750_000_000


@dataclass
class TokenView:
    """Single-token contrarian deep-dive: honest bull vs bear + playbook."""

    symbol: str
    name: str
    price: float
    change_24h: float
    market_cap: float
    fdv_mc: Optional[float]
    ath_change_pct: float
    vol_mc: Optional[float]
    funding_rate: Optional[float] = None
    open_interest_usd: Optional[float] = None
    bull: list[str] = field(default_factory=list)
    bear: list[str] = field(default_factory=list)
    playbook: list[str] = field(default_factory=list)
    score: int = 50                # 0–100 conviction
    stance: str = "NEUTRAL"


def build_token_view(t: TokenMetrics, *, funding: Optional[float] = None,
                     open_interest_usd: Optional[float] = None) -> TokenView:
    """Derive bull/bear factors + a conviction read from a token's metrics.

    Deliberately surfaces the risks ("gw ga mau cuma shill") — every bear point
    is a real red flag computed from the data, never softened.
    """
    fdv_mc, vol_mc = t.fdv_mc, t.vol_mc
    sig = funding_signal(funding)
    bull, bear = [], []

    if sig == "BULLISH":
        bull.append(f"Funding {funding:+.3f}% BULLISH — shorts crowded, bahan bakar squeeze")
    if fdv_mc is not None and fdv_mc <= 1.2:
        bull.append(f"FDV/MC {fdv_mc:.1f}x — supply hampir full, nyaris tanpa unlock pressure")
    if t.ath_change_pct <= -80:
        bull.append(f"{t.ath_change_pct:.0f}% dari ATH — downside udah banyak ke-flush")
    if vol_mc is not None and 0.1 <= vol_mc <= 3:
        bull.append(f"turnover {vol_mc * 100:.0f}% mcap — likuiditas cukup buat masuk/keluar")
    if -10 <= t.change_24h <= 4:
        bull.append("harga konsolidasi/pullback — timing entry lebih enak")

    if fdv_mc is not None and fdv_mc >= 2:
        bear.append(f"FDV/MC {fdv_mc:.1f}x — supply unlock masih besar, tekanan jual menekan")
    if sig == "BEARISH":
        bear.append(f"Funding {funding:+.3f}% overheated — longs crowded, rawan long squeeze")
    if vol_mc is not None and vol_mc < 0.05:
        bear.append("likuiditas tipis — slippage & exit susah")
    if vol_mc is not None and vol_mc > 3:
        bear.append(f"volume {vol_mc:.1f}x mcap — sinyal wash/artificial")
    if t.change_24h > 25:
        bear.append(f"udah pump +{t.change_24h:.1f}% — rawan FOMO trap & koreksi")
    if t.ath_change_pct > -20:
        bear.append("deket ATH — risk/reward kurang menarik")

    score = max(0, min(100, 50 + 12 * len(bull) - 12 * len(bear)))
    if score >= 65:
        stance = "ACCUMULATE (conviction)"
    elif score >= 45:
        stance = "NEUTRAL — tunggu konfirmasi"
    else:
        stance = "AVOID — risiko tinggi"

    playbook = ["Conviction play, BUKAN momentum trade" if score >= 60
                else "Tunggu setup lebih bersih — jangan maksa"]
    if t.market_cap < SMALL_CAP_MAX:
        playbook.append("Sizing kecil + DCA (masih bisa turun)")
    if sig == "BEARISH" or t.change_24h > 25:
        playbook.append("NO leverage — rawan kena likuidasi")
    playbook.append("Horizon: mingguan–bulanan, bukan harian")

    return TokenView(
        symbol=t.symbol, name=t.name, price=t.price, change_24h=t.change_24h,
        market_cap=t.market_cap, fdv_mc=fdv_mc, ath_change_pct=t.ath_change_pct,
        vol_mc=vol_mc, funding_rate=funding, open_interest_usd=open_interest_usd,
        bull=bull, bear=bear, playbook=playbook, score=score, stance=stance,
    )


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
