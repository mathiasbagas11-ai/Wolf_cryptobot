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
