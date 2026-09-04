"""Tests for scoring whale-veto policies over the signals that were recorded."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from wolf.config import Settings
from wolf.models import Signal, Status
from wolf.tracker import OUTCOMES_KEY, Tracker
from wolf.whatif import render_whale, whale_report


def _outcome(rows, *, r: float, stance: str, n: int, risk_pct: float = 1.0) -> None:
    entry = 100.0
    start = datetime.now(timezone.utc) - timedelta(hours=6)
    rows.append(Signal(
        symbol=f"BTC{n}", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=entry * 1.05, sl=entry * (1 - risk_pct / 100),
        strategy="SCALP", whale_stance=stance,
        status=Status.TP_HIT.value if r > 0 else Status.SL_HIT.value,
        pnl_pct=r * risk_pct, r_multiple=r,
        activated_at=start.isoformat(),
        exit_time=(start + timedelta(hours=1)).isoformat(),
        resolved_at=(start + timedelta(hours=1)).isoformat(),
    ).to_dict())


def _tracker(store, fake_client, tracker_settings, rows) -> Tracker:
    store.write(OUTCOMES_KEY, rows)
    return Tracker(store, fake_client, tracker_settings)


def _book(each=8):
    """A book where WITH loses and AGAINST wins, each side with a real spread.

    The spread matters: a stance whose outcomes all landed on one number has
    no variance, and `mean_gap` refuses to quote a gap it cannot put an error
    on. A fixture without it would test the refusal, not the comparison.
    """
    rows = []
    n = 0
    for i in range(each):
        _outcome(rows, r=-1.0 if i % 4 else 0.5, stance="WITH", n=n); n += 1
        _outcome(rows, r=1.0 if i % 4 else -1.0, stance="AGAINST", n=n); n += 1
    return rows


def test_dropping_the_losing_stance_lifts_the_book(store, fake_client, tracker_settings):
    rows = _book()
    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    by = {r["label"]: r for r in report["results"]}

    assert by["live"]["kept"] == 16
    assert by["drop WITH"]["kept"] == 8
    assert by["drop WITH"]["mean_r"] > by["live"]["mean_r"]
    assert by["drop AGAINST"]["mean_r"] < by["live"]["mean_r"]


def test_a_policy_is_scored_against_what_it_would_have_dropped(
    store, fake_client, tracker_settings
):
    """Two policies hold different subsets, so nothing about them is paired.

    A geometry re-cut re-scores the same trade and the market move cancels; a
    veto decides which trades exist and nothing cancels. The contrast is
    therefore Welch's two-sample t over two disjoint populations.
    """
    report = whale_report(_tracker(store, fake_client, tracker_settings, _book()))
    gap = next(c for c in report["contrasts"] if c["label"] == "drop WITH")

    assert gap["n_a"] == 8 and gap["n_b"] == 8      # kept vs dropped, disjoint
    assert gap["gap_r"] > 0                          # what was kept did better
    assert 0 < gap["df"] < gap["n"] - 1              # Welch, not pooled n-1
    assert "p_adj" in gap


def test_live_has_no_contrast_because_it_drops_nothing(
    store, fake_client, tracker_settings
):
    report = whale_report(_tracker(store, fake_client, tracker_settings, _book()))
    assert "live" not in {c["label"] for c in report["contrasts"]}
    assert next(r for r in report["results"] if r["label"] == "live")["dropped"] == 0


def test_each_policy_is_costed_on_its_own_risk_unit(store, fake_client, tracker_settings):
    """Dropping a stance changes the mix of stop distances, and cost is bps/risk.

    Charging every policy the whole book's median would rank them on a cost
    none of them would have paid.
    """
    rows = []
    n = 0
    for _ in range(8):
        _outcome(rows, r=-1.0, stance="WITH", n=n, risk_pct=0.4); n += 1
        _outcome(rows, r=1.0, stance="AGAINST", n=n, risk_pct=4.0); n += 1
    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    by = {r["label"]: r for r in report["results"]}

    # WITH stops at 0.4% pay 0.50R; AGAINST stops at 4.0% pay 0.05R.
    assert by["drop WITH"]["cost_r"] < by["drop AGAINST"]["cost_r"]


def test_noise_separates_from_nothing(store, fake_client, tracker_settings):
    rows = []
    for i in range(16):
        _outcome(rows, r=1.0 if i % 2 else -1.0,
                 stance="WITH" if i % 3 else "AGAINST", n=i)
    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    assert not any(c.get("fdr_survives") for c in report["contrasts"])
    assert "no policy separates" in render_whale(report)


def test_the_card_always_states_what_it_cannot_see(store, fake_client, tracker_settings):
    """The veto's own rejects never became signals, so they are not in the ledger.

    That is the same self-blinding the AI layer was kept out of the signal path
    to avoid, arriving through a gate that was never held to the rule. The
    reading the numbers most invite — "the veto filters the wrong side" — is
    exactly the one they cannot support, so the caveat prints every time.
    """
    digest = render_whale(
        whale_report(_tracker(store, fake_client, tracker_settings, _book()))
    )
    assert "BLIND SPOT" in digest
    assert "before they are recorded" in digest


def test_too_thin_a_book_says_so(store, fake_client, tracker_settings):
    rows = []
    _outcome(rows, r=1.0, stance="WITH", n=0)
    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    assert report["error"]
    assert "WHATIF whale:" in render_whale(report)


def test_it_is_reachable_from_telegram(store, fake_client, tracker_settings):
    from wolf.notify.commands import CommandRouter

    app = SimpleNamespace(
        analyze=None, account=None, learning=None,
        tracker=_tracker(store, fake_client, tracker_settings, _book()),
        settings=Settings(), screener=SimpleNamespace(_validator=None),
    )
    reply = CommandRouter(app).handle("/whatif whale")
    assert "WHATIF whale" in reply and "BLIND SPOT" in reply
    assert "/whatif whale" in CommandRouter(app).handle("/help")


def test_two_policies_that_keep_the_same_trades_are_one_test(
    store, fake_client, tracker_settings
):
    """With only WITH and AGAINST present, "drop WITH" and "only AGAINST" agree.

    Printing both reports one finding twice, and entering both into the
    correction raises the bar every other row must clear on the strength of a
    duplicate.
    """
    report = whale_report(_tracker(store, fake_client, tracker_settings, _book()))
    labels = [r["label"] for r in report["results"]]

    assert "drop WITH" in labels
    assert "only AGAINST" not in labels          # identical selection, collapsed
    assert len(report["contrasts"]) == 2         # drop WITH, drop AGAINST


def test_a_policy_that_refuses_nothing_present_collapses_into_live(
    store, fake_client, tracker_settings
):
    """A row that drops nothing repeats the live row and answers nothing."""
    report = whale_report(_tracker(store, fake_client, tracker_settings, _book()))
    assert "drop NEUTRAL" not in [r["label"] for r in report["results"]]


def test_a_stance_that_is_present_still_gets_its_own_row(
    store, fake_client, tracker_settings
):
    """Collapsing must key on the selection, not on the policy name."""
    rows = _book(each=6)
    for i in range(6):
        _outcome(rows, r=0.2, stance="NEUTRAL", n=100 + i)
    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    labels = [r["label"] for r in report["results"]]

    assert "drop NEUTRAL" in labels
    assert "only AGAINST" in labels   # now distinct from "drop WITH"


# ── the discount, and the confound that decides whether any of it means anything ──


def _overlapping(rows) -> list:
    """Make every trade run concurrently, so mean_open is high."""
    start = datetime.now(timezone.utc) - timedelta(hours=6)
    for r in rows:
        r["activated_at"] = start.isoformat()
        r["exit_time"] = (start + timedelta(hours=5)).isoformat()
        r["resolved_at"] = r["exit_time"]
    return rows


def test_the_contrast_is_charged_for_positions_held_side_by_side(
    store, fake_client, tracker_settings
):
    """The defect this fixes: a gap quoted on the nominal count.

    The digest prints eff_n_floor two lines below saying the sample is a third
    the size, and leaves the reader to reconcile them — which on the one row
    anybody would act on is not a reconciliation anyone performs.
    """
    report = whale_report(
        _tracker(store, fake_client, tracker_settings, _overlapping(_book(each=20)))
    )
    gap = next(c for c in report["contrasts"] if c["label"] == "drop WITH")

    assert report["overlap"] > 1.0
    assert abs(gap["t"]) < abs(gap["t_nominal"])     # the discount bites
    assert gap["eff_n"] < gap["n"]
    assert gap["se_r"] > gap["se_nominal"]


def test_no_overlap_leaves_the_statistic_alone(store, fake_client, tracker_settings):
    """Sequential trades are what the nominal count already assumes."""
    from wolf.stats import mean_gap

    a, b = [1.0, 0.5, 1.2, 0.8], [-1.0, -0.5, -1.2, -0.8]
    assert mean_gap(a, b, 1.0)["t"] == mean_gap(a, b, 0.4)["t"]  # never inflates


def test_the_within_strategy_gap_separates_a_stance_from_a_strategy_mix(
    store, fake_client, tracker_settings
):
    """The confound the codebase already names, applied to the contrast.

    Here WITH and the losing strategy are the *same trades*: PREDUMP always
    loses and is always WITH, SCALP always wins and is never WITH. The pooled
    gap looks decisive and reproduces inside neither strategy, because there is
    nothing to compare within either one.
    """
    rows = []
    n = 0
    for i in range(10):
        _outcome(rows, r=-1.0 if i % 4 else 0.5, stance="WITH", n=n); n += 1
        rows[-1]["strategy"] = "PREDUMP"
        _outcome(rows, r=1.0 if i % 4 else -1.0, stance="AGAINST", n=n); n += 1
        rows[-1]["strategy"] = "SCALP"

    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    pooled = next(c for c in report["contrasts"] if c["label"] == "drop WITH")
    assert pooled["gap_r"] > 0                       # looks like a whale effect

    # But no strategy can reproduce it: each holds only one stance.
    assert all(row["gap"] is None for row in report["by_strategy"])
    assert "too few on one side to compare" in render_whale(report)


def test_a_real_stance_effect_survives_inside_the_strategies(
    store, fake_client, tracker_settings
):
    """The other side of the same check — an effect present within each strategy."""
    rows = []
    n = 0
    for strategy in ("SCALP", "PREDUMP"):
        for i in range(10):
            _outcome(rows, r=-1.0 if i % 4 else 0.5, stance="WITH", n=n)
            rows[-1]["strategy"] = strategy
            n += 1
            _outcome(rows, r=1.0 if i % 4 else -1.0, stance="AGAINST", n=n)
            rows[-1]["strategy"] = strategy
            n += 1

    report = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    inside = [r["gap"] for r in report["by_strategy"] if r["gap"]]
    assert len(inside) == 2
    assert all(g["gap_r"] > 0 for g in inside)
    assert "reappears inside 2 of 2 strategies" in render_whale(report)


def test_a_window_reads_one_era_instead_of_pooling_them_all(
    store, fake_client, tracker_settings
):
    """The cost gate changed which trades exist, so an unwindowed card pools bots."""
    rows = _book(each=6)
    old = datetime.now(timezone.utc) - timedelta(days=20)
    for r in rows[:6]:
        r["resolved_at"] = old.isoformat()
        r["exit_time"] = r["resolved_at"]

    everything = whale_report(_tracker(store, fake_client, tracker_settings, rows))
    recent = whale_report(
        _tracker(store, fake_client, tracker_settings, rows), window_hours=48
    )
    assert recent["sample"] < everything["sample"]
    assert "window=48h" in render_whale(recent)
    assert "all eras" in render_whale(everything)


def test_the_window_is_reachable_from_telegram(store, fake_client, tracker_settings):
    from wolf.notify.commands import CommandRouter

    app = SimpleNamespace(
        analyze=None, account=None, learning=None,
        tracker=_tracker(store, fake_client, tracker_settings, _book()),
        settings=Settings(), screener=SimpleNamespace(_validator=None),
    )
    router = CommandRouter(app)
    assert "window=48h" in router.handle("/whatif whale 48")
    assert "all eras" in router.handle("/whatif whale")
    assert "Usage" in router.handle("/whatif whale banyak")
