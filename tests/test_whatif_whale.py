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
