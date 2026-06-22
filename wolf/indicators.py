"""Technical indicators.

Pure functions over price series — no I/O, no globals, no hidden state. Each
takes a list of floats (or :class:`~wolf.models.Candle`) and returns a number or
list. This makes them deterministic and trivial to unit-test, in contrast to the
old code where indicator math was interleaved with data fetching in the 11k-line
monolith.
"""

from __future__ import annotations

from typing import Sequence

from wolf.models import Candle


def closes(candles: Sequence[Candle]) -> list[float]:
    return [c.close for c in candles]


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average. Returns a series aligned to ``values``."""
    if not values or period <= 0:
        return []
    k = 2 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: Sequence[float], period: int) -> float:
    if len(values) < period or period <= 0:
        return float("nan")
    return sum(values[-period:]) / period


def rsi(values: Sequence[float], period: int = 14) -> float:
    """Wilder's RSI over the last ``period`` deltas. Returns 0-100 (50 if flat)."""
    if len(values) <= period:
        return float("nan")
    gains = 0.0
    losses = 0.0
    # Seed with the first ``period`` changes.
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing over the remainder.
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    """Average True Range over the last ``period`` candles."""
    if len(candles) <= period:
        return float("nan")
    trs: list[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    if len(trs) < period:
        return float("nan")
    return sum(trs[-period:]) / period


def rsi_series(values: Sequence[float], period: int = 14) -> list[float]:
    """RSI at every bar (aligned to ``values``); leading bars are NaN.

    Useful for divergence detection where the RSI trajectory matters, not just
    its latest value.
    """
    n = len(values)
    out = [float("nan")] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        return 100 - (100 / (1 + g / l))

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def bollinger_bands(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[float, float, float, float]:
    """Return ``(upper, middle, lower, width)`` for the latest bar.

    ``width`` is ``(upper - lower) / middle`` — a normalised band width used to
    detect a volatility *squeeze* (consolidation before a breakout). All NaN if
    there is insufficient data.
    """
    if len(values) < period or period <= 0:
        nan = float("nan")
        return (nan, nan, nan, nan)
    window = list(values[-period:])
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = variance ** 0.5
    upper = mid + mult * std
    lower = mid - mult * std
    width = (upper - lower) / mid if mid else float("nan")
    return (upper, mid, lower, width)


def bb_width_series(values: Sequence[float], period: int = 20, mult: float = 2.0) -> list[float]:
    """Normalised Bollinger band width at every bar (leading bars NaN)."""
    n = len(values)
    out = [float("nan")] * n
    for i in range(period - 1, n):
        _, _, _, width = bollinger_bands(values[: i + 1], period, mult)
        out[i] = width
    return out


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """Return ``(macd_line, signal_line, histogram)`` for the latest bar."""
    if len(values) < slow + signal:
        return (float("nan"), float("nan"), float("nan"))
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_series = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_series = ema(macd_series, signal)
    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    return (macd_line, signal_line, macd_line - signal_line)


def volume_ratio(candles: Sequence[Candle], lookback: int = 20) -> float:
    """Latest candle volume divided by the average of the prior ``lookback``."""
    if len(candles) < lookback + 1:
        return float("nan")
    recent = candles[-1].volume
    baseline = sum(c.volume for c in candles[-lookback - 1 : -1]) / lookback
    if baseline == 0:
        return float("nan")
    return recent / baseline


def adx(candles: Sequence[Candle], period: int = 14) -> tuple[float, float, float]:
    """Wilder's ADX with directional indicators.

    Returns ``(adx, plus_di, minus_di)`` for the latest bar — the trend-strength
    and direction inputs the regime filter uses. All NaN when there is not enough
    data (need at least ``2*period`` candles for ADX to stabilise).
    """
    n = len(candles)
    nan = float("nan")
    if n < 2 * period + 1:
        return (nan, nan, nan)

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        cur, prev = candles[i], candles[i - 1]
        up = cur.high - prev.high
        down = prev.low - cur.low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        ))

    # Wilder-smoothed TR / +DM / -DM seeded with the first ``period`` sums.
    atr_s = sum(trs[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dxs: list[float] = []
    for i in range(period, len(trs)):
        atr_s = atr_s - atr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dm[i]
        minus_s = minus_s - minus_s / period + minus_dm[i]
        if atr_s == 0:
            dxs.append(0.0)
            continue
        pdi = 100 * plus_s / atr_s
        mdi = 100 * minus_s / atr_s
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)

    if len(dxs) < period:
        return (nan, nan, nan)
    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    # +DI / -DI for the latest bar (atr_s/plus_s/minus_s hold the final smoothings).
    plus_di = 100 * plus_s / atr_s if atr_s else nan
    minus_di = 100 * minus_s / atr_s if atr_s else nan
    return (adx_val, plus_di, minus_di)
