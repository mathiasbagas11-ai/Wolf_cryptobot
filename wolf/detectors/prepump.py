"""Pre-pump detector.

Looks for quiet accumulation *before* a breakout up — the spirit of the old
``detect_prepump``, expressed on a single candle series:

* **Bollinger squeeze** — band width compressed into the bottom of its recent
  distribution (consolidation precedes expansion).               (30 pts)
* **Volume coil** — a fresh volume spike after a quiet stretch.   (25 pts)
* **OI/PA proxy → momentum** — rising RSI from neutral + bullish MACD. (20 pts)
* **Money flow** — bullish RSI divergence (hidden accumulation).  (15 pts)
* **Trend context** — price above its EMA50.                      (10 pts)
* **Value proximity** — the breakout has not yet run away from VWAP. (15 pts)
* **Bullish FvG below** — demand-zone support under the setup.    (10 pts)

Funding-rate and open-interest inputs from the original are intentionally left
out of the candle-only contract; they can be layered in by a richer detector
without touching this one.

Why the scoring was rebalanced
------------------------------
The detector emitted nothing for months, and the reason was arithmetic rather
than market conditions: the mandatory breakout gate below and several of the
scored components could not be true on the same bar, so the reachable ceiling
sat under the threshold.

* **VWAP discount** paid 15 points for ``price <= vwap``, while the gate
  demands a close above the prior consolidation high *and* above EMA50. On a
  breakout out of a base, VWAP sits back in the base — the two conditions are
  each other's negation. It now pays for price being *near* value
  (``vwap_premium_max``) instead of below it, which is the check that actually
  separates the first breakout bar from a bar chasing a move already 50% old.
* **Quiet accumulation** paid 12 points for ``not is_expanding``, which the
  volume-coil award on the same bar contradicts. Removed; the footprint it
  described belongs to the bars *before* the breakout, which this detector does
  not score.
* **RSI 50-68** paid 20 points for a band a breakout bar rarely sits in — a
  close above a 10-bar high on 1.8x volume prints RSI in the seventies. The
  band is now 50-80, which keeps the "not yet vertical" intent without ruling
  out the bar the gate requires.
* **The squeeze test** compared the pre-breakout width against the *minimum* of
  the last 20 widths within 15%. Inside a base that lasts weeks that window
  sits entirely inside the base, where the width oscillates with nothing to
  contrast against, so being within 15% of its own 20-bar minimum on the exact
  pre-breakout bar was closer to a coin flip than a measurement. Widening the
  window does not repair it: with 150 candles in hand and a base occupying most
  of them, there is no longer history left to call the base "compressed"
  against, and any percentile cut passes that fraction of the base's bars by
  construction.

  So compression is no longer what gates the detector. What separates a coil
  releasing from an ordinary breakout is measurable without that contrast: the
  breakout bar's range against the range of the bars it broke out of. A base is
  quiet by definition, so a genuine release prints a bar several times the size
  of anything in it — the expansion is the event, and it is scale-free and
  indifferent to how long the base ran. Compression is still *scored*, on the
  percentile test, where a coin flip costs points instead of the whole signal.

Together those made 78 unreachable in practice: the honest ceiling on a real
breakout bar was 73. The threshold is back to the documented 65.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from wolf import indicators as ind
from wolf import structure as struct
from wolf.config import LadderSettings
from wolf import orderflow
from wolf.detectors.base import (
    DEFAULT_LADDER,
    Detector,
    SignalCandidate,
    build_targets,
)
from wolf.models import Candle


def _percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of ``values`` (``q`` in 0..1)."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


class PrePumpDetector(Detector):
    name = "PREPUMP"
    timeframe = "1h"
    min_candles = 60

    #: The coil release and the compression that preceded it are what make this
    #: a pre-pump rather than a breakout. The rest is context.
    primary_components = ("bollinger_squeeze", "volume_coil")

    #: A squeeze breakout is entered on the bar that resolves it, so the entry
    #: is worth chasing further than a mean-reversion setup would be — the
    #: screener's global cap is sized for the latter and dropped these outright.
    max_chase_r = 1.5

    def __init__(
        self,
        score_threshold: int = 65,
        squeeze_percentile: float = 0.25,
        squeeze_window: int = 100,
        expansion_mult: float = 2.5,
        vwap_premium_max: float = 0.03,
        ladder: LadderSettings = DEFAULT_LADDER,
        flow_veto: bool = True,
    ) -> None:
        self.score_threshold = score_threshold
        # The pre-breakout width must sit in the bottom ``squeeze_percentile``
        # of the last ``squeeze_window`` widths — "compressed relative to its
        # own recent history", measured over a window long enough to contain
        # more than the base itself.
        self.squeeze_percentile = squeeze_percentile
        self.squeeze_window = squeeze_window
        # The breakout bar's range must be this multiple of the typical range
        # of the bars it broke out of — the coil releasing, as opposed to the
        # next bar of a market that was already moving.
        self.expansion_mult = expansion_mult
        # How far above VWAP the breakout may close and still count as bought
        # near value rather than chased.
        self.vwap_premium_max = vwap_premium_max
        self.ladder = ladder
        self.flow_veto = flow_veto

    def _squeezed(self, bb_widths_seq: Sequence[float]) -> bool:
        """True when the bar *before* the breakout was compressed.

        Measured on the bars leading up to the breakout, excluding the current
        candle: the breakout itself widens the bands, so including it would
        cancel the very thing being tested.
        """
        window = bb_widths_seq[-(self.squeeze_window + 1):-1]
        prior = [w for w in window if not math.isnan(w)]
        if len(prior) < 20:
            return False
        cutoff = _percentile(prior, self.squeeze_percentile)
        return bool(cutoff > 0 and prior[-1] <= cutoff)

    def evaluate(
        self, symbol: str, candles: Sequence[Candle], context=None, features=None
    ) -> Optional[SignalCandidate]:
        if not self._ready(candles):
            return None

        if features is not None and features.valid:
            price = features.price
            atr = features.atr
            rsi = features.rsi
            hist = features.macd_hist
            vr = features.vol_ratio
            ema50_last = features.ema50_last
            bb_widths_seq = features.bb_widths
            if math.isnan(hist) or math.isnan(ema50_last):
                return None
        else:
            closes = ind.closes(candles)
            price = closes[-1]
            atr = ind.atr(candles, 14)
            rsi = ind.rsi(closes, 14)
            _, _, hist = ind.macd(closes)
            if any(math.isnan(x) for x in (atr, rsi, hist)) or atr <= 0:
                return None
            vr = ind.volume_ratio(candles, 20)
            ema50 = ind.ema(closes, 50)
            ema50_last = ema50[-1] if ema50 else float("nan")
            bb_widths_seq = ind.bb_width_series(closes, 20)

        # Hard gate: EMA20 > EMA50 AND EMA50 rising — filters dead-cat bounces.
        # Pre-pump accumulation requires an established uptrend, not just a temporary cross.
        if math.isnan(ema50_last):
            return None
        closes_seq = ind.closes(candles)
        ema20_series = ind.ema(closes_seq, 20)
        ema50_series = ind.ema(closes_seq, 50)
        if not ema20_series or not ema50_series or len(ema50_series) < 15:
            return None
        ema20_last_val = ema20_series[-1]
        if ema20_last_val <= ema50_last:
            return None  # not in uptrend
        if price <= ema50_last:
            return None  # price below EMA50 — not in the right zone
        if ema50_series[-1] <= ema50_series[-10]:
            return None  # EMA50 declining — dead-cat bounce, not accumulation

        # Hard gate: breakout confirmation. The 0/8 failure mode was buying
        # *mid-squeeze* with no proof the move had started — price would chop or
        # break the other way. Require the latest candle to close above the prior
        # consolidation high on a bullish bar, i.e. the squeeze is resolving UP.
        last = candles[-1]
        window = candles[-11:-1]
        cons_high = max(c.high for c in window)
        cons_low = min(c.low for c in window)
        if not (last.close > cons_high and last.close > last.open):
            return None

        # Hard gate: the coil has to actually release. A base is quiet by
        # definition, so the bar that ends one is several times the size of
        # anything inside it. A breakout bar merely the size of its neighbours
        # is the next bar of a market that was already moving — MOMENTUM's
        # setup, not this one, and letting it through here double-counts it.
        prior_ranges = sorted(c.high - c.low for c in window)
        mid = len(prior_ranges) // 2
        base_range = (prior_ranges[mid] if len(prior_ranges) % 2
                      else (prior_ranges[mid - 1] + prior_ranges[mid]) / 2)
        last_range = last.high - last.low
        if base_range <= 0 or last_range < base_range * self.expansion_mult:
            return None

        score = 0
        reasons: list[str] = []
        parts: dict[str, int] = {}

        def award(name: str, points: int, reason: str = "") -> None:
            """Score a component and remember that it is what did the scoring.

            A total says nothing about its composition, and the components here
            are not interchangeable: this detector's band edges were calibrated
            on replayed shapes rather than trades, so which of them a signal
            actually leant on is the first thing the first real trades should be
            asked about.
            """
            nonlocal score
            score += points
            parts[name] = parts.get(name, 0) + points
            if reason:
                reasons.append(reason)

        # 1. Bollinger squeeze — measured on the bars LEADING UP TO the breakout
        #    (excluding the current candle, which naturally widens the bands), so
        #    "was squeezed, now breaking out" scores instead of being cancelled.
        #    Scored rather than gated: see the module docstring on why a
        #    percentile inside a long base cannot carry a hard decision.
        if self._squeezed(bb_widths_seq):
            award("bollinger_squeeze", 30,
                  "Bollinger squeeze resolving — breakout from consolidation")
        reasons.append(
            f"Coil released: breakout range {last_range / base_range:.1f}x the base"
        )

        # 2. Volume coil. This detector deliberately does not run the shared
        #    directional gate: a pre-pump is by definition still flat, so
        #    demanding a price move would reject the very setup it looks for.
        #    What it does demand is that the expansion is not being *sold* —
        #    a squeeze that releases downward is a breakdown, and the size-only
        #    test scored it identically to accumulation.
        state = orderflow.analyse(candles)
        buyers_lead = state.buy_share == state.buy_share and state.buy_share > 0.52
        sellers_lead = state.buy_share == state.buy_share and state.buy_share < 0.48
        if state.is_expanding and sellers_lead and self.flow_veto:
            return None

        if not math.isnan(vr) and vr >= 1.8:
            award("volume_coil", 25,
                  f"Coil released on the bid: {vr:.1f}x volume, "
                  f"{state.buy_share * 100:.0f}% taker buys"
                  if buyers_lead else f"Volume coil released: {vr:.1f}x average")
        elif not math.isnan(vr) and vr >= 1.3:
            award("volume_building", 12, f"Volume building: {vr:.1f}x average")

        # 3. Momentum — MACD positive already enforced as hard gate above;
        #    reward when RSI is also in the building zone (50-80). Above 80 the
        #    bar is already vertical and the setup reads as a chase.
        if 50 <= rsi < 80:
            award("momentum_zone", 20,
                  f"Momentum building: RSI {rsi:.0f} in accumulation zone, MACD positive")
        else:
            award("macd_positive", 8, f"MACD positive, RSI {rsi:.0f}")

        # 4. Money flow — bullish divergence
        div = struct.rsi_divergence(candles, lookback=25)
        if div.bull_score >= 10:
            award("bull_divergence", 15, "Bullish RSI divergence — hidden accumulation")

        # 5. Trend context
        if not math.isnan(ema50_last) and price > ema50_last:
            award("trend_context", 10, "Price above EMA50 — uptrend context")

        # 6. Value proximity — the breakout has not yet run away from VWAP.
        #    A squeeze resolving *at* fair value is the entry; the same squeeze
        #    50% later is somebody else's exit liquidity.
        vwap_val = ind.vwap(candles, lookback=50)
        if not math.isnan(vwap_val) and vwap_val > 0:
            premium = price / vwap_val - 1
            if premium <= self.vwap_premium_max:
                award("value_proximity", 15,
                      f"Breaking out at value — {premium * 100:+.1f}% vs VWAP {vwap_val:.6g}")

        # 7. Bullish FvG below price — structural support under the setup (+10)
        fvgs = ind.find_fvgs(candles, lookback=50)
        bull_fvg = next((g for g in fvgs if g["type"] == "BULL" and g["top"] <= price), None)
        if bull_fvg:
            award("bull_fvg_below", 10,
                  f"Bullish FvG below ({bull_fvg['bottom']:.6g}–{bull_fvg['top']:.6g}) — demand zone support")

        # 8. Derivatives confluence (optional) — negative funding = crowded
        #    shorts ripe for a squeeze; rising OI = fresh positioning.
        if context is not None:
            if context.funding_extreme_squeeze:
                award("funding_extreme", 15,
                      f"Funding extreme {context.funding_rate:.3f}% — short squeeze imminent")
            elif context.funding_squeeze:
                award("funding_negative", 10,
                      f"Funding negative {context.funding_rate:.3f}% — short squeeze potential")
            if context.oi_rising:
                award("oi_rising", 8,
                      f"OI rising {context.oi_change_pct:+.1f}% — accumulation")

        if score < self.score_threshold:
            return None

        sl, tp, ladder = build_targets(price, atr, is_long=True, sl_mult=1.5, ladder_cfg=self.ladder)
        # Structural stop below the squeeze base — a flat ATR stop is too tight in
        # the low-volatility squeeze and gets wicked out before the expansion.
        sl = min(sl, cons_low - atr * 0.2)
        if not (tp > price > sl):
            return None
        return SignalCandidate(
            symbol=symbol,
            signal_type="PREPUMP",
            direction="LONG",
            entry_price=price,
            tp=tp,
            sl=sl,
            score=min(score, 100),
            strategy=self.name,
            reasons=reasons,
            confluence_level="HIGH" if score >= 85 else "MEDIUM",
            timeframe=self.timeframe,
            entry_mode="MOMENTUM_NOW",
            tps=ladder,
            max_chase_r=self.max_chase_r,
            score_parts=parts,
        )
