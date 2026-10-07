"""Pre-computed candle features shared across all detectors in one scan cycle.

Building indicators once per symbol (instead of once per detector) eliminates
the 5x redundant RSI / ATR / MACD / volume-ratio computation that previously ran
independently inside every detector.  CandleFeatures is passed as an optional
fourth argument to Detector.evaluate(); detectors fall back to computing inline
when it is None so existing unit tests require no changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from wolf import indicators as ind
from wolf.models import Candle


@dataclass(frozen=True)
class CandleFeatures:
    """Immutable bag of pre-computed indicators for one candle set.

    Produced once per symbol per screener cycle by :meth:`build` and shared
    across all five detectors.  Only the scalar values that every detector
    actually consumes are stored here; specialised computations (liquidity
    sweeps, RSI divergence) still run inside the detector that needs them.
    """

    price: float              # latest close
    atr: float                # ATR(14)
    rsi: float                # RSI(14) at latest bar
    macd_hist: float          # MACD(12,26,9) histogram at latest bar
    vol_ratio: float          # latest volume / 20-bar average
    ema20_last: float         # EMA(20) at latest bar
    ema50_last: float         # EMA(50) at latest bar
    bb_width_now: float       # normalised Bollinger width at latest bar
    bb_widths: tuple[float, ...]  # full BB width series — PREPUMP squeeze check

    @property
    def valid(self) -> bool:
        """True when the core scalar indicators are usable (no NaN, ATR > 0).

        Deliberately excludes macd_hist / ema20_last / ema50_last because those
        need more candles to converge; each detector checks the values it uses.
        """
        return (
            not math.isnan(self.rsi)
            and not math.isnan(self.atr)
            and not math.isnan(self.vol_ratio)
            and self.atr > 0
        )

    @classmethod
    def build(cls, candles: Sequence[Candle]) -> "CandleFeatures":
        """Compute all indicators in a single pass over ``candles``."""
        closes = ind.closes(candles)
        price = closes[-1] if closes else float("nan")
        atr_val = ind.atr(candles, 14)
        rsi_val = ind.rsi(closes, 14)
        _, _, hist = ind.macd(closes)
        vr = ind.volume_ratio(candles, 20)
        ema20 = ind.ema(closes, 20)
        ema50 = ind.ema(closes, 50)
        bb_widths_list = ind.bb_width_series(closes, 20)
        _, _, _, bb_now = ind.bollinger_bands(closes, 20)
        return cls(
            price=price,
            atr=atr_val,
            rsi=rsi_val,
            macd_hist=hist,
            vol_ratio=vr,
            ema20_last=ema20[-1] if ema20 else float("nan"),
            ema50_last=ema50[-1] if ema50 else float("nan"),
            bb_width_now=bb_now,
            bb_widths=tuple(bb_widths_list),
        )
