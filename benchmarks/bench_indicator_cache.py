"""Benchmark: shared indicator cache vs independent per-detector computation.

Measures the real wall-clock and CPU-time savings from CandleFeatures when
the screener runs all 5 detectors over the full 15-symbol universe.

Run from the repo root:
    python benchmarks/bench_indicator_cache.py
"""

from __future__ import annotations

import random
import sys
import time
import timeit
from pathlib import Path

# Make sure wolf package is importable from repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from wolf.detectors import default_detectors
from wolf.indicator_cache import CandleFeatures
from wolf.models import Candle
from wolf.screener import DEFAULT_UNIVERSE

# ── synthetic candle generation ───────────────────────────────────────────────

def make_candles(n: int = 150, seed: int = 42) -> list[Candle]:
    """Generate ``n`` realistic-ish candles with a random walk and volume noise."""
    rng = random.Random(seed)
    price = 30_000.0
    candles: list[Candle] = []
    for i in range(n):
        change = rng.gauss(0, price * 0.005)
        open_ = price
        close = price + change
        high = max(open_, close) + abs(rng.gauss(0, price * 0.002))
        low  = min(open_, close) - abs(rng.gauss(0, price * 0.002))
        volume = rng.uniform(500, 3000)
        candles.append(Candle(
            time=i * 900_000,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
        ))
        price = close
    return candles


# One candle set per symbol (different seeds simulate different symbols).
UNIVERSE_CANDLES: dict[str, list[Candle]] = {
    sym: make_candles(150, seed=i)
    for i, sym in enumerate(DEFAULT_UNIVERSE)
}

DETECTORS = default_detectors()


# ── benchmark targets ─────────────────────────────────────────────────────────

def run_without_cache() -> int:
    """Baseline: each detector computes its own indicators from scratch."""
    signals = 0
    for symbol, candles in UNIVERSE_CANDLES.items():
        for detector in DETECTORS:
            # features=None forces every detector to compute inline (old path).
            result = detector.evaluate(symbol, candles, context=None, features=None)
            if result:
                signals += 1
    return signals


def run_with_cache() -> int:
    """Optimised: build CandleFeatures once, share across all detectors."""
    signals = 0
    for symbol, candles in UNIVERSE_CANDLES.items():
        features = CandleFeatures.build(candles)
        for detector in DETECTORS:
            result = detector.evaluate(symbol, candles, context=None, features=features)
            if result:
                signals += 1
    return signals


# ── runner ────────────────────────────────────────────────────────────────────

def bench(fn, label: str, repeat: int = 200) -> tuple[float, float, float]:
    """Return (min_ms, mean_ms, max_ms) over ``repeat`` iterations."""
    times_ns: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        fn()
        t1 = time.perf_counter_ns()
        times_ns.append(t1 - t0)
    to_ms = 1e-6
    mn  = min(times_ns) * to_ms
    avg = (sum(times_ns) / len(times_ns)) * to_ms
    mx  = max(times_ns) * to_ms
    return mn, avg, mx


def fmt_row(label: str, mn: float, avg: float, mx: float) -> str:
    return f"  {label:<22}  min={mn:6.2f}ms  avg={avg:6.2f}ms  max={mx:6.2f}ms"


def main() -> None:
    n_sym     = len(DEFAULT_UNIVERSE)
    n_det     = len(DETECTORS)
    repeat    = 200
    warmup    = 10

    print(f"\nWolf Cryptobot — Indicator Cache Benchmark")
    print(f"{'─'*58}")
    print(f"  Universe :  {n_sym} symbols")
    print(f"  Detectors:  {n_det}  ({', '.join(d.name for d in DETECTORS)})")
    print(f"  Candles  :  150 per symbol  (15m × 150 = 37.5 h of history)")
    print(f"  Repeats  :  {repeat} full-universe cycles (+ {warmup} warmup)")
    print(f"{'─'*58}\n")

    # Warmup — JIT / caches settle.
    for _ in range(warmup):
        run_without_cache()
        run_with_cache()

    # Verify both paths produce the same signal count.
    sigs_old = run_without_cache()
    sigs_new = run_with_cache()
    match = "✓ match" if sigs_old == sigs_new else f"✗ MISMATCH ({sigs_old} vs {sigs_new})"
    print(f"  Signal count sanity check: {sigs_old} signals — {match}\n")

    print("  Running benchmark …")
    mn_old, avg_old, mx_old = bench(run_without_cache, "without cache", repeat)
    mn_new, avg_new, mx_new = bench(run_with_cache,    "with cache",    repeat)

    speedup_avg = avg_old / avg_new if avg_new > 0 else float("inf")
    speedup_min = mn_old  / mn_new  if mn_new  > 0 else float("inf")
    saved_avg   = avg_old - avg_new
    saved_pct   = (saved_avg / avg_old * 100) if avg_old > 0 else 0

    print()
    print(fmt_row("Without cache (baseline)", mn_old, avg_old, mx_old))
    print(fmt_row("With cache (optimised)",   mn_new, avg_new, mx_new))
    print()
    print(f"  {'─'*54}")
    print(f"  Speed-up (avg)   : {speedup_avg:.2f}×  ({saved_pct:.1f}% faster)")
    print(f"  Time saved / cycle: {saved_avg:.2f} ms avg")

    # Per-detector breakdown: how many indicator calls are removed?
    # Each of the 5 detectors used to call: rsi, atr, vol_ratio, macd (4 fns)
    # + prepump adds: bb_width_series, bollinger_bands, ema(50) = 3 extra
    # + swing adds: ema(20), ema(50) = 2 extra  (ema50 shared with prepump)
    # With cache: all of those collapse to 1 CandleFeatures.build() call.
    indicators_per_sym_old = (
        5   # rsi   — every detector
        + 5 # atr   — every detector
        + 4 # vol_ratio — all except swing (which doesn't use it explicitly; still in cache)
        + 3 # macd  — momentum, prepump (scalp/swing/predump skip it)
        + 1 # bb_width_series — prepump only
        + 1 # bollinger_bands — prepump only
        + 1 # ema(50) — prepump + swing
        + 1 # ema(20) — swing only
    )
    indicators_per_sym_new = 1  # CandleFeatures.build() covers everything
    print()
    print(f"  Indicator fn calls per symbol:")
    print(f"    Before : ~{indicators_per_sym_old} calls × {n_sym} symbols = ~{indicators_per_sym_old * n_sym} calls/cycle")
    print(f"    After  : ~{indicators_per_sym_new} build call × {n_sym} symbols = ~{indicators_per_sym_new * n_sym} calls/cycle")
    print(f"    Removed: {(indicators_per_sym_old - indicators_per_sym_new) * n_sym} redundant indicator calls per cycle")
    print()
    # Project over a running day (screener runs every 10 min = 144 cycles/day)
    cycles_per_day = 144
    ms_saved_day = saved_avg * cycles_per_day
    print(f"  Projected over 1 day ({cycles_per_day} screener cycles):")
    print(f"    CPU time saved: {ms_saved_day:.0f} ms  (~{ms_saved_day/1000:.1f} s / day)")
    print(f"{'─'*58}\n")


if __name__ == "__main__":
    main()
