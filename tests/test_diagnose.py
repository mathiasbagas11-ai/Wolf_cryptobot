"""Tests for the diagnostics layer — the statistics behind a verdict."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.diagnose import (
    CONCLUSIVE_T, EDGE_NEGATIVE, EDGE_POSITIVE, INCONCLUSIVE,
    MIN_CONCLUSIVE_TRADES, NET_NEGATIVE_AFTER_COST, diagnose, render_digest,
)
from wolf.models import Signal, Status
from wolf.tracker import OUTCOMES_KEY, Tracker


def _outcome(store_list, *, r: float, status: str, strategy: str = "SCALP",
             risk_pct: float = 1.0, symbol: str = "BTCUSDT", n: int = 0) -> None:
    """Append a resolved outcome with an exact R-multiple and risk distance."""
    entry = 100.0
    sl = entry * (1 - risk_pct / 100)
    start = datetime.now(timezone.utc) - timedelta(hours=6)
    store_list.append(Signal(
        symbol=f"{symbol}{n}", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=entry * 1.05, sl=sl, strategy=strategy,
        tp_ladder=[{"level": 1, "price": entry * 1.01}, {"level": 2, "price": entry * 1.02}],
        status=status, pnl_pct=r * risk_pct, r_multiple=r,
        activated_at=start.isoformat(),
        exit_time=(start + timedelta(hours=1)).isoformat(),
        resolved_at=(start + timedelta(hours=1)).isoformat(),
    ).to_dict())


def _tracker_with(store, fake_client, tracker_settings, rows) -> Tracker:
    store.write(OUTCOMES_KEY, rows)
    return Tracker(store, fake_client, tracker_settings)


def test_small_sample_is_never_conclusive(store, fake_client, tracker_settings):
    """A tiny, uniformly excellent sample still buys no verdict."""
    rows = []
    for i in range(10):
        _outcome(rows, r=2.0, status=Status.TP_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert diag["overall"]["n"] == 10
    assert diag["overall"]["mean_r"] == 2.0
    assert diag["overall"]["verdict"] == INCONCLUSIVE


def test_noisy_large_sample_is_inconclusive(store, fake_client, tracker_settings):
    """Enough trades, but the mean is buried in its own spread."""
    rows = []
    for i in range(MIN_CONCLUSIVE_TRADES + 20):
        # Alternating +2R/-2R: mean ~0, huge sd.
        _outcome(rows, r=2.0 if i % 2 else -2.0, status=Status.TP_HIT.value if i % 2
                 else Status.SL_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert abs(diag["overall"]["t"]) < CONCLUSIVE_T
    assert diag["overall"]["verdict"] == INCONCLUSIVE


def test_consistent_losses_are_called(store, fake_client, tracker_settings):
    rows = []
    for i in range(MIN_CONCLUSIVE_TRADES + 20):
        _outcome(rows, r=-1.0 if i % 4 else -0.8, status=Status.SL_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert diag["overall"]["verdict"] == EDGE_NEGATIVE


def test_gross_edge_that_costs_do_not_survive(store, fake_client, tracker_settings):
    """A real but thin edge is reported net, not gross.

    risk_pct=0.4 makes 1R = 0.4%, so a 20bps round trip is 0.5R — larger than
    the +0.1R gross edge.
    """
    rows = []
    for i in range(MIN_CONCLUSIVE_TRADES + 20):
        _outcome(rows, r=0.11 if i % 2 else 0.09, status=Status.TP_HIT.value,
                 risk_pct=0.4, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows),
                    round_trip_bps=20.0)
    assert diag["overall"]["mean_r"] > 0          # positive gross...
    assert diag["cost"]["cost_r"] == 0.5
    assert diag["overall"]["net_r"] < 0           # ...negative net
    assert diag["overall"]["verdict"] == NET_NEGATIVE_AFTER_COST


def test_wide_risk_makes_cost_negligible(store, fake_client, tracker_settings):
    rows = []
    for i in range(MIN_CONCLUSIVE_TRADES + 20):
        _outcome(rows, r=0.55 if i % 2 else 0.45, status=Status.TP_HIT.value,
                 risk_pct=4.0, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows),
                    round_trip_bps=20.0)
    assert diag["cost"]["cost_r"] == 0.05
    assert diag["overall"]["verdict"] == EDGE_POSITIVE


def test_no_edge_baseline_comes_from_the_ladder_not_fifty_percent(
    store, fake_client, tracker_settings
):
    """A 33% win rate is good or bad depending entirely on the geometry.

    Ladder here: SL 1% below entry, rungs at +1% and +2%. Under a driftless walk
    P(TP1 first) = 1/(1+1) = 50%, and P(full | TP1) = 1/2, so the all-or-nothing
    baseline is 25% — not 50%.
    """
    rows = []
    for i in range(40):
        _outcome(rows, r=2.0 if i < 10 else -1.0,
                 status=Status.TP_HIT.value if i < 10 else Status.SL_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    b = diag["by_strategy"]["SCALP"]
    assert b["win_rate"] == 25.0
    assert b["no_edge_win_rate"] == 25.0
    assert b["win_rate_z"] == 0.0   # exactly what no edge predicts


def test_tp1_banking_raises_the_no_edge_baseline(store, fake_client, tracker_settings):
    """When TP1 banks a win, merely reaching TP1 is a win — a lower bar."""
    rows = []
    for i in range(40):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, n=i)
    tracker = _tracker_with(store, fake_client, tracker_settings, rows)
    strict = diagnose(tracker, tp1_banks_win=False)["by_strategy"]["SCALP"]
    lenient = diagnose(tracker, tp1_banks_win=True)["by_strategy"]["SCALP"]
    assert lenient["no_edge_win_rate"] > strict["no_edge_win_rate"]
    assert lenient["no_edge_win_rate"] == 50.0


def test_concurrency_floors_the_effective_sample(store, fake_client, tracker_settings):
    """Overlapping positions are not independent observations."""
    rows = []
    for i in range(20):
        _outcome(rows, r=1.0, status=Status.TP_HIT.value, n=i)  # all share one window
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert diag["concurrency"]["max_open"] == 20
    assert diag["concurrency"]["eff_n_floor"] < 20


def test_flags_surface_known_configuration_problems(store, fake_client, tracker_settings):
    rows = []
    for i in range(5):
        _outcome(rows, r=1.0, status=Status.TP_HIT.value, n=i)
    _outcome(rows, r=0.0, status=Status.EXPIRED.value, n=99)  # unpriced exit
    diag = diagnose(
        _tracker_with(store, fake_client, tracker_settings, rows),
        state_dir="state_data", ai_available=False, tp1_banks_win=False,
    )
    assert "STATE_NOT_PERSISTED" in diag["flags"]
    assert "AI_CLIENT_UNAVAILABLE" in diag["flags"]
    assert "AI_NEVER_DECIDES" in diag["flags"]
    assert "TP1_BANKS_WIN_OFF" in diag["flags"]
    assert "UNPRICED_EXITS=1" in diag["flags"]


def test_digest_is_compact_and_carries_the_evidence(store, fake_client, tracker_settings):
    rows = []
    for i in range(30):
        _outcome(rows, r=1.0 if i % 3 else -1.0,
                 status=Status.TP_HIT.value if i % 3 else Status.SL_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    text = render_digest(diag)
    assert text.startswith("WOLF-DIAG v1")
    # The verdict and the numbers needed to argue with it travel together.
    for token in ("sample", "cost", "overall", "meanR=", "sdR=", "t=", "ci95=",
                  "netR=", "noedge_wr=", "concur", "SCALP"):
        assert token in text, token
    assert len(text.splitlines()) < 25   # pasteable whole


def test_empty_history_does_not_explode(store, fake_client, tracker_settings):
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, []))
    assert diag["overall"]["verdict"] == INCONCLUSIVE
    assert diag["sample"]["traded"] == 0
    assert render_digest(diag).startswith("WOLF-DIAG v1")


def _cost_outcome(strategy: str, r: float, risk_pct: float) -> dict:
    """A resolved outcome with an explicit R and stop distance."""
    entry = 100.0
    return {
        "symbol": "XUSDT", "signal_type": strategy, "direction": "LONG",
        "strategy": strategy, "entry_price": entry, "sl": entry * (1 - risk_pct / 100),
        "tp": entry * 1.05, "status": "TP_HIT" if r > 0 else "SL_HIT",
        "pnl_pct": r * risk_pct, "r_multiple": r, "activated": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "exit_time": "2026-01-01T06:00:00+00:00",
        "resolved_at": "2026-01-01T06:00:00+00:00",
    }


def test_cost_is_charged_per_strategy_not_portfolio_wide(store, fake_client):
    """A tight-stop strategy pays far more in R than a wide-stop one.

    Charging every strategy the portfolio median inverts their ranking: TIGHT
    looks like the winner on gross R, but a 20bps round trip is 1.67R of its
    own risk unit against 0.25R of WIDE's.
    """
    from wolf.config import TrackerSettings
    from wolf.diagnose import diagnose
    from wolf.tracker import Tracker

    rows = [_cost_outcome("TIGHT", 0.62, 0.12) for _ in range(10)]
    rows += [_cost_outcome("WIDE", 0.30, 0.80) for _ in range(10)]
    store.write("signal_outcomes", rows)

    diag = diagnose(Tracker(store, fake_client, TrackerSettings()), round_trip_bps=20.0)
    tight = diag["by_strategy"]["TIGHT"]
    wide = diag["by_strategy"]["WIDE"]

    assert tight["mean_r"] > wide["mean_r"]        # TIGHT wins on gross R
    assert round(tight["cost_r"], 2) == 1.67       # ...and pays 1.67R to trade
    assert round(wide["cost_r"], 2) == 0.25
    assert tight["net_r"] < wide["net_r"]          # the ranking flips net of cost
    assert tight["net_r"] < 0 < wide["net_r"]


def test_breakeven_win_rate_uses_realised_wins_not_the_ladder_ceiling(store, fake_client):
    """The ceiling flatters the requirement by roughly half.

    A 1:3 ladder that scales out 50% at 1R and trails to breakeven has a 1.7R
    ceiling — but a winner that only reaches TP1 banks +0.5R. Against -1R
    losses that needs a ~67% win rate, not the ~37% the ceiling implies.
    """
    from wolf.config import TrackerSettings
    from wolf.diagnose import diagnose
    from wolf.tracker import Tracker

    rows = [_cost_outcome("SWING", 0.5, 1.0) for _ in range(8)]     # TP1-only wins
    rows += [_cost_outcome("SWING", -1.0, 1.0) for _ in range(11)]  # full stops
    store.write("signal_outcomes", rows)

    lad = diagnose(Tracker(store, fake_client, TrackerSettings()))["ladder"]
    assert lad["avg_win_r"] == 0.5
    assert lad["avg_loss_r"] == 1.0
    assert round(lad["breakeven_win_rate"]) == 67     # 1.0 / 1.5


def test_an_average_winner_above_the_ladder_ceiling_is_flagged(store, fake_client, tracker_settings):
    """Scaling out caps a perfect run, so a bigger average winner cannot be real.

    The 1:3 ladder sells 50% at 1R and 30% at 2R, leaving 20% to collect 3R —
    1.7R for a flawless trade. An average winner above that is a grading fault,
    and it is the kind that flatters every number downstream, so the digest has
    to say it out loud rather than report a strong quarter.
    """
    rows = []
    for i in range(12):
        _outcome(rows, r=3.0, status=Status.TP_HIT.value, n=i)
    for i in range(12, 20):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert diag["ladder"]["avg_win_r"] == 3.0
    flag = next(f for f in diag["flags"] if f.startswith("AVG_WIN_ABOVE_LADDER_CEILING"))
    assert "3.00R>1.70R" in flag
    assert flag in render_digest(diag)


def test_a_winner_inside_the_ceiling_is_not_flagged(store, fake_client, tracker_settings):
    rows = []
    for i in range(12):
        _outcome(rows, r=1.7, status=Status.TP_HIT.value, n=i)
    for i in range(12, 20):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert not any(f.startswith("AVG_WIN_ABOVE_LADDER_CEILING") for f in diag["flags"])


def test_a_silent_ai_names_the_fault_that_silenced_it(store, fake_client, tracker_settings):
    """ABSTAIN is the shape every AI fault takes, so the count alone says nothing.

    A rejected key, a spent balance and a model that cannot emit JSON all
    degrade to the same verdict — deliberately, so the layer can never block
    screening. The digest has to carry the reason, or the only way to tell them
    apart is to go reading container logs.
    """
    rows = []
    for i in range(12):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, n=i)
    for i, row in enumerate(rows):
        row["ai_verdict"] = "ABSTAIN"
        row["ai_rationale"] = (
            "ABSTAIN/NO_JSON: HTTP 402: Insufficient Balance" if i < 9
            else "ABSTAIN/ERROR: Timeout"
        )
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    flag = next(f for f in diag["flags"] if f.startswith("AI_NEVER_DECIDES"))
    assert flag == (
        "AI_NEVER_DECIDES(NO_JSON: HTTP 402: Insufficient Balance x9 | ERROR: Timeout x3)"
    )
    assert flag in render_digest(diag)


def test_a_silent_ai_with_no_reason_recorded_still_flags(store, fake_client, tracker_settings):
    """Signals recorded before the reason was tracked must not break the flag."""
    rows = []
    for i in range(12):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, n=i)
    for row in rows:
        row["ai_verdict"] = "ABSTAIN"
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert "AI_NEVER_DECIDES" in diag["flags"]


def test_whale_stance_is_crossed_with_strategy(store, fake_client, tracker_settings):
    """One dimension cannot tell a whale effect from the strategy mix.

    Here every trade loses when whales agree and wins when they disagree — but
    only because the losing strategy is the one whales happen to agree with.
    The flat by_whale_stance split reads as a strong whale signal; the cross-tab
    shows each strategy performing the same whichever side the whales took, so
    the signal is the strategy, not the whales.
    """
    rows = []
    for i in range(8):                      # PREDUMP loses, whales agree with it
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, strategy="PREDUMP", n=i)
        rows[-1]["whale_stance"] = "WITH"
    for i in range(8, 16):                  # MOMENTUM wins, whales oppose it
        _outcome(rows, r=1.0, status=Status.TP_HIT.value, strategy="MOMENTUM", n=i)
        rows[-1]["whale_stance"] = "AGAINST"
    # A few of each strategy on the other side, performing the same as its peers.
    for i in range(16, 20):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, strategy="PREDUMP", n=i)
        rows[-1]["whale_stance"] = "AGAINST"
    for i in range(20, 24):
        _outcome(rows, r=1.0, status=Status.TP_HIT.value, strategy="MOMENTUM", n=i)
        rows[-1]["whale_stance"] = "WITH"

    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    cross = diag["whale_by_strategy"]
    # Within a strategy the stance makes no difference — the flat split lied.
    assert cross["PREDUMP"]["WITH"]["mean_r"] == cross["PREDUMP"]["AGAINST"]["mean_r"] == -1.0
    assert cross["MOMENTUM"]["WITH"]["mean_r"] == cross["MOMENTUM"]["AGAINST"]["mean_r"] == 1.0
    digest = render_digest(diag)
    assert "wxs:PREDUMP" in digest and "wxs:MOMENTUM" in digest


def test_the_cross_tab_stays_out_of_the_way_when_it_cannot_inform(store, fake_client, tracker_settings):
    """A strategy whose trades all share one stance says nothing — omit it."""
    rows = []
    for i in range(10):
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, strategy="PREDUMP", n=i)
        rows[-1]["whale_stance"] = "WITH"
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert "wxs:" not in render_digest(diag)


def test_a_unanimous_bucket_is_not_reported_as_no_evidence(store, fake_client, tracker_settings):
    """Three trades all at exactly -1.0R gave sd=0, se=0, and therefore t=0.

    A t of zero is how "the mean is indistinguishable from nothing" is written,
    which is the opposite of what a unanimous sample says. The ladder quantises
    its outcomes, so buckets landing on one value are ordinary, not exotic.
    """
    rows = []
    for i in range(20):                       # a spread for the sample to borrow
        _outcome(rows, r=1.5 if i % 2 else -1.0, status=Status.TP_HIT.value if i % 2
                 else Status.SL_HIT.value, strategy="SCALP", n=i)
    for i in range(20, 23):                   # SWING: three identical stop-outs
        _outcome(rows, r=-1.0, status=Status.SL_HIT.value, strategy="SWING", n=i)

    swing = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))["by_strategy"]["SWING"]
    assert swing["sd_r"] == 0.0               # its own spread really is zero
    assert swing["t"] < -1.0                  # but the verdict is not "nothing"


def test_near_identical_outcomes_do_not_manufacture_certainty(store, fake_client, tracker_settings):
    """Two wins at 0.499 and 0.500 produced t=999 — proof of an edge from n=2.

    sd was 0.001, so the standard error all but vanished. Nothing about those
    two trades justifies more confidence than twenty varied ones.
    """
    rows = []
    for i in range(20):
        _outcome(rows, r=1.5 if i % 2 else -1.0, status=Status.TP_HIT.value if i % 2
                 else Status.SL_HIT.value, strategy="SCALP", n=i)
    _outcome(rows, r=0.499, status=Status.TP_HIT.value, strategy="PREDUMP", n=20)
    _outcome(rows, r=0.500, status=Status.TP_HIT.value, strategy="PREDUMP", n=21)

    pre = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))["by_strategy"]["PREDUMP"]
    assert pre["t"] < 1.0
    assert pre["verdict"] == INCONCLUSIVE


def test_a_genuinely_narrow_spread_is_left_alone(store, fake_client, tracker_settings):
    """The floor must catch collapse, not quietly widen every honest estimate."""
    rows = []
    for i in range(20):
        _outcome(rows, r=1.5 if i % 2 else -1.0, status=Status.TP_HIT.value if i % 2
                 else Status.SL_HIT.value, strategy="SCALP", n=i)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    scalp = diag["by_strategy"]["SCALP"]
    # SCALP is the whole sample here, so its spread is its own — untouched.
    assert scalp["sd_r"] == diag["overall"]["sd_r"]
    assert scalp["t"] == diag["overall"]["t"]


# ── multiplicity control ────────────────────────────────────────────────────


def _mixed_strategies(rows, names=("SCALP", "SWING", "MOMENTUM", "TRAP")) -> None:
    """One bucket per strategy, each with a spread the t-statistic can read."""
    n = 0
    for name in names:
        for i in range(12):
            r = 1.5 if i % 3 else -1.0
            _outcome(rows, r=r, strategy=name, n=n,
                     status=Status.TP_HIT.value if r > 0 else Status.SL_HIT.value)
            n += 1


def test_every_printed_bucket_carries_an_adjusted_p(store, fake_client, tracker_settings):
    """The correction has to reach the row, not just the module."""
    rows = []
    _mixed_strategies(rows)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))

    for bucket in diag["by_strategy"].values():
        assert "p_raw" in bucket and "p_adj" in bucket
        assert bucket["p_adj"] >= bucket["p_raw"] - 1e-9
        assert isinstance(bucket["fdr_survives"], bool)


def test_the_pre_registered_question_stays_out_of_the_family(
    store, fake_client, tracker_settings
):
    """``overall`` was fixed before any data arrived; the buckets are a search.

    Penalising the one question the bot was built to answer for the dozen
    subgroup splits it never ran would be the wrong correction applied to the
    wrong hypothesis.
    """
    rows = []
    _mixed_strategies(rows)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    assert "p_adj" not in diag["overall"]


def test_a_wider_family_makes_the_same_bucket_harder_to_believe(
    store, fake_client, tracker_settings
):
    """The finding the whole change exists to deliver.

    An identical SCALP bucket, with identical trades and an identical t, comes
    out less believable when it was one of four splits than when it was the
    only one — because it was.
    """
    def _scalp_padj(extra_noise_buckets):
        rows = []
        n = 0
        # The bucket under test: a consistent, convincing-looking SCALP sample.
        for i in range(12):
            r = 1.5 if i % 3 else -1.0
            _outcome(rows, r=r, strategy="SCALP", n=n,
                     status=Status.TP_HIT.value if r > 0 else Status.SL_HIT.value)
            n += 1
        # The other splits a reader scans past. Alternating +1/-1 is pure
        # noise, so these contribute nothing but their own existence — which
        # is exactly the cost multiplicity control is meant to charge.
        for b in range(extra_noise_buckets):
            for i in range(12):
                r = 1.0 if i % 2 else -1.0
                _outcome(rows, r=r, strategy=f"NOISE{b}", n=n,
                         status=Status.TP_HIT.value if r > 0 else Status.SL_HIT.value)
                n += 1
        diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
        scalp = diag["by_strategy"]["SCALP"]
        return scalp["p_adj"], scalp["t"], scalp["p_raw"]

    alone_p, alone_t, alone_raw = _scalp_padj(0)
    crowded_p, crowded_t, crowded_raw = _scalp_padj(5)

    # The same trades, so the same statistic and the same uncorrected p.
    assert alone_t == crowded_t
    assert alone_raw == crowded_raw
    # But a weaker claim once it is one of several splits that were looked at.
    assert crowded_p > alone_p


def test_a_bucket_with_no_degrees_of_freedom_claims_nothing(
    store, fake_client, tracker_settings
):
    """One trade is not a finding, however extreme."""
    rows = []
    _outcome(rows, r=5.0, status=Status.TP_HIT.value, strategy="PREPUMP", n=0)
    for i in range(12):
        _outcome(rows, r=1.5 if i % 3 else -1.0, strategy="SCALP", n=i + 1,
                 status=Status.TP_HIT.value if i % 3 else Status.SL_HIT.value)
    diag = diagnose(_tracker_with(store, fake_client, tracker_settings, rows))

    prepump = diag["by_strategy"]["PREPUMP"]
    assert prepump["n"] == 1
    assert prepump["p_raw"] == 1.0
    assert prepump["fdr_survives"] is False


def test_the_digest_prints_the_adjusted_p_and_says_what_it_is(
    store, fake_client, tracker_settings
):
    """A correction the reader has to remember to apply is not a correction."""
    rows = []
    _mixed_strategies(rows)
    digest = render_digest(
        diagnose(_tracker_with(store, fake_client, tracker_settings, rows))
    )
    assert "padj=" in digest
    assert "fdr " in digest
    assert "overall is not in the family" in digest
