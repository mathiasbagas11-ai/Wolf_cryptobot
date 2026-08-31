"""Small statistical primitives the diagnostic needs and the stdlib lacks.

Two functions, both pure Python: there is no scipy here and adding one for two
formulas would be a poor trade on a container this size.

The Benjamini-Hochberg procedure is ported from HKUDS/Vibe-Trading
(``agent/src/quantlib/multipletesting.py``, MIT licence), reimplemented without
numpy. The reason it is here at all is that the digest reports roughly a dozen
buckets, each with its own t-statistic, and a reader scanning them for the
convincing one is running a dozen tests while judging each at the bar for one.
Under a null where nothing has any edge, eleven buckets at p<0.05 produce about
one apparent finding per digest -- roughly seven a week, every one of them
noise. Controlling the false discovery rate cuts that by about four fifths and,
more usefully, puts an adjusted number next to the row so the correction does
not depend on the reader remembering to apply it.
"""

from __future__ import annotations

import math

#: Benjamini-Hochberg operates on the family of tests actually looked at, so
#: the rate is a property of the report rather than of any one bucket.
DEFAULT_FDR = 0.05


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    # The continued fraction converges quickly only on one side of this point;
    # the symmetry I_x(a,b) = 1 - I_{1-x}(b,a) covers the other.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_to_p(t: float, df: int) -> float:
    """Two-sided p-value for a t-statistic on ``df`` degrees of freedom.

    Student's t rather than a normal approximation, because the buckets this
    serves are small — a handful of trades apiece — and at df below about 30 the
    normal tail is materially thinner. Substituting it would shrink every
    p-value, which is the one direction a correction against false findings must
    never err in.

    Returns 1.0 when the statistic carries no information (no spread, no
    degrees of freedom), which is the honest reading: a bucket whose outcomes
    all landed on the same rung has not measured anything.
    """
    if df < 1 or not math.isfinite(t):
        return 1.0
    if t == 0.0:
        return 1.0
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def benjamini_hochberg(
    p_values: list[float], fdr: float = DEFAULT_FDR
) -> tuple[list[bool], list[float]]:
    """Control the false discovery rate across a family of tests.

    Bonferroni controls the chance of *any* false positive and is too strict
    here: with a dozen buckets it would demand a bar almost nothing reaches, and
    a diagnostic that can never report anything is as useless as one that
    reports everything. BH instead bounds the expected *fraction* of the
    rejections that are false, which is the quantity a reader scanning a table
    actually cares about.

    Args:
        p_values: Raw p-values, one per bucket, in the caller's order.
        fdr: Rate to control at.

    Returns:
        ``(rejected, adjusted)``, both in the caller's original order.
        ``adjusted`` is the BH-adjusted p-value, monotone in the raw one and
        clipped to 1.0, so it can be printed next to a bucket directly.
    """
    n = len(p_values)
    if n == 0:
        return [], []
    if not 0.0 < fdr < 1.0:
        raise ValueError(f"fdr must be in (0, 1), got {fdr}")

    order = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [1.0] * n
    running = 1.0
    # Adjusted values are the running minimum taken from the largest rank down,
    # so they cannot decrease as the raw p-value increases.
    for rank in range(n, 0, -1):
        i = order[rank - 1]
        running = min(running, n / rank * p_values[i])
        adjusted[i] = min(1.0, running)

    # Step-up: the largest rank clearing its own threshold, and everything
    # below it, is rejected.
    cutoff = 0
    for rank in range(1, n + 1):
        if p_values[order[rank - 1]] <= rank / n * fdr:
            cutoff = rank
    rejected = [False] * n
    for rank in range(cutoff):
        rejected[order[rank]] = True
    return rejected, adjusted
