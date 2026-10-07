"""Tests for the statistical primitives behind the digest's multiplicity control."""

from __future__ import annotations

import random

import pytest

from wolf.stats import DEFAULT_FDR, benjamini_hochberg, t_to_p


# ── t to p ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "t, df, expected",
    [
        (2.0, 10, 0.0734),
        (2.0, 100, 0.0483),
        (2.571, 5, 0.0500),     # the 5% critical value on 5 df
        (3.0, 20, 0.0071),
        (1.0, 5, 0.3632),
        (1.96, 1_000_000, 0.0500),  # converges on the normal in the limit
    ],
)
def test_the_two_sided_p_matches_the_t_table(t, df, expected):
    assert t_to_p(t, df) == pytest.approx(expected, abs=1e-3)


def test_the_p_value_is_symmetric_in_the_sign_of_t():
    """Two-sided, so a bucket losing convincingly is as significant as one winning."""
    assert t_to_p(2.3, 12) == pytest.approx(t_to_p(-2.3, 12))


def test_a_statistic_carrying_no_information_scores_one():
    """The honest reading for a bucket that has not measured anything.

    A bucket whose outcomes all landed on the same rung has no spread, hence
    t=0 — and a degenerate one may arrive with no degrees of freedom at all.
    Neither is evidence, and neither may be allowed to look like it.
    """
    assert t_to_p(0.0, 5) == 1.0
    assert t_to_p(2.0, 0) == 1.0
    assert t_to_p(float("inf"), 5) == 1.0


def test_the_student_tail_is_fatter_than_the_normal_at_small_df():
    """Why this is not a normal approximation.

    Substituting the normal would shrink every p-value, which is the one
    direction a guard against false findings must never err in. At 5 degrees
    of freedom the gap is more than double.
    """
    assert t_to_p(2.0, 5) > t_to_p(2.0, 100) > t_to_p(2.0, 1_000_000)
    assert t_to_p(2.0, 5) > 2 * t_to_p(2.0, 1_000_000)


# ── Benjamini-Hochberg ──────────────────────────────────────────────────────


#: Benjamini & Hochberg (1995), Table 1 — the paper's own worked example,
#: which rejects the first four hypotheses at an FDR of 0.05.
_BH95 = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
         0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.0000]


def test_the_procedure_reproduces_the_original_paper():
    rejected, _ = benjamini_hochberg(_BH95, 0.05)
    assert [i for i, r in enumerate(rejected) if r] == [0, 1, 2, 3]


def test_results_come_back_in_the_caller_s_order():
    """Buckets are rendered in their own order, so the answer must follow it."""
    rejected, adjusted = benjamini_hochberg(_BH95, 0.05)
    order = list(range(len(_BH95)))
    random.Random(1).shuffle(order)
    shuffled = [_BH95[i] for i in order]

    s_rejected, s_adjusted = benjamini_hochberg(shuffled, 0.05)
    assert [s_rejected[j] for j in range(len(order))] == [rejected[order[j]] for j in range(len(order))]
    assert s_adjusted == pytest.approx([adjusted[order[j]] for j in range(len(order))])


def test_an_adjusted_p_never_undersells_the_raw_one():
    _, adjusted = benjamini_hochberg(_BH95, 0.05)
    assert all(a >= p - 1e-12 for a, p in zip(adjusted, _BH95))
    assert all(a <= 1.0 for a in adjusted)


def test_adjusted_values_are_monotone_in_the_raw_ones():
    """A bucket with a worse raw p can never come out with a better adjusted p."""
    _, adjusted = benjamini_hochberg(_BH95, 0.05)
    assert adjusted == sorted(adjusted)  # _BH95 is already sorted ascending


def test_the_correction_bites_hardest_on_a_large_family():
    """The whole point: the same p-value means less when more were looked at."""
    _, few = benjamini_hochberg([0.04, 0.5], 0.05)
    _, many = benjamini_hochberg([0.04] + [0.5] * 11, 0.05)
    assert many[0] > few[0]


def test_a_lone_test_needs_no_correction():
    rejected, adjusted = benjamini_hochberg([0.04], 0.05)
    assert rejected == [True]
    assert adjusted == pytest.approx([0.04])


def test_an_empty_family_is_not_an_error():
    """A deployment with no buckets yet still has to render its digest."""
    assert benjamini_hochberg([], DEFAULT_FDR) == ([], [])


def test_a_nonsensical_rate_is_refused():
    with pytest.raises(ValueError):
        benjamini_hochberg([0.01], 0.0)
    with pytest.raises(ValueError):
        benjamini_hochberg([0.01], 1.0)
