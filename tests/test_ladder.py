"""Tests for the target geometry — the 1:3 policy and its ladder maths."""

from __future__ import annotations

import os
from unittest import mock

from wolf.config import LadderSettings, Settings
from wolf.detectors.base import build_targets, ladder_from_risk


# ── placement ────────────────────────────────────────────────────────────
def test_final_rung_sits_at_the_target_ratio():
    ladder = ladder_from_risk(100.0, 5.0, is_long=True)
    assert [r["price"] for r in ladder] == [105.0, 110.0, 115.0]
    assert (ladder[-1]["price"] - 100.0) / 5.0 == 3.0
    assert [r["r_multiple"] for r in ladder] == [1.0, 2.0, 3.0]


def test_placement_mirrors_for_shorts():
    ladder = ladder_from_risk(100.0, 5.0, is_long=False)
    assert [r["price"] for r in ladder] == [95.0, 90.0, 85.0]
    assert (100.0 - ladder[-1]["price"]) / 5.0 == 3.0


def test_ratio_holds_for_any_stop_distance():
    """A structural stop that has to be widened must widen the targets too."""
    for risk in (0.5, 2.0, 7.5, 31.0):
        ladder = ladder_from_risk(100.0, risk, is_long=True)
        assert round((ladder[-1]["price"] - 100.0) / risk, 6) == 3.0


def test_allocations_close_the_whole_position():
    assert sum(r["allocation"] for r in ladder_from_risk(100.0, 5.0, True)) == 1.0


def test_degenerate_inputs_produce_no_ladder():
    assert ladder_from_risk(100.0, 0.0, True) == []
    assert ladder_from_risk(0.0, 5.0, True) == []


def test_build_targets_wraps_an_atr_stop():
    sl, tp, ladder = build_targets(100.0, 2.0, is_long=True, sl_mult=1.5)
    assert sl == 97.0                       # 1.5 ATR below entry
    assert (tp - 100.0) / (100.0 - sl) == 3.0
    assert len(ladder) == 3


def test_custom_ratio_is_honoured():
    ladder = ladder_from_risk(100.0, 5.0, True, LadderSettings(rr_target=5.0))
    assert (ladder[-1]["price"] - 100.0) / 5.0 == 5.0


# ── the numbers that make a ratio interpretable ───────────────────────────
def test_full_run_r_accounts_for_scaling_out():
    """1:3 does not mean a winner returns 3R once size comes off early."""
    assert LadderSettings().full_run_r == 1.7   # .5*1R + .3*2R + .2*3R


def test_breakeven_win_rate_follows_the_ladder_not_the_headline_ratio():
    assert round(LadderSettings().breakeven_win_rate, 1) == 37.0   # 100/(1+1.7)
    # Letting the whole position ride to 3R needs a much lower win rate — and
    # gives up every partial win on the way.
    all_at_the_end = LadderSettings(tp_ladder_fractions=(1.0,), tp_allocations=(1.0,))
    assert all_at_the_end.full_run_r == 3.0
    assert round(all_at_the_end.breakeven_win_rate, 1) == 25.0


# ── allocation repair ─────────────────────────────────────────────────────
def test_allocations_resize_to_a_shorter_ladder():
    weights = LadderSettings().allocations_for(2)
    assert round(sum(weights), 6) == 1.0
    assert weights[0] > weights[1]        # keeps the front-loaded shape


def test_allocations_handle_a_ladder_longer_than_the_config():
    weights = LadderSettings().allocations_for(5)
    assert len(weights) == 5 and round(sum(weights), 6) == 1.0


def test_allocations_of_an_empty_ladder():
    assert LadderSettings().allocations_for(0) == ()


# ── environment wiring ────────────────────────────────────────────────────
def test_ratio_is_configurable_from_the_environment():
    with mock.patch.dict(os.environ, {"RISK_RR_TARGET": "4", "RISK_TP_ALLOCATIONS": "0.6,0.4"}):
        ladder = Settings.from_env().ladder
    assert ladder.rr_target == 4.0
    assert ladder.tp_allocations == (0.6, 0.4)


def test_malformed_allocations_fall_back_to_the_default():
    with mock.patch.dict(os.environ, {"RISK_TP_ALLOCATIONS": "half,quarter"}):
        ladder = Settings.from_env().ladder
    assert ladder.tp_allocations == (0.5, 0.3, 0.2)


def test_tp1_banks_win_is_on_by_default():
    """At 1:3, "reached TP1 then came back" is the common shape of a winner."""
    assert Settings.from_env().tracker.tp1_banks_win is True


def test_min_signal_rr_defends_the_policy():
    assert Settings.from_env().min_signal_rr == 2.5
