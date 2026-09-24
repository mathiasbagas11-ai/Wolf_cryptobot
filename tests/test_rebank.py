"""Tests for re-booking outcomes whose banked rungs were never counted.

Three exit paths close a scaled position and only two priced it as scaled. The
timeout path valued the whole position at the price on the clock, as though the
slice sold at TP1 were still open — under-booking a trade that drifted back and
over-booking one parked above the rung, and in the dead-band case filing it as
having said nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.account import ACCOUNT_KEY, PaperAccount
from wolf.config import TrackerSettings
from wolf.learning.engine import MEMORY_KEY
import pytest

from wolf.models import Signal, Status
from wolf.rebank import rebank_outcomes, replay_balance
from wolf.tracker import OUTCOMES_KEY

_LADDER = [
    {"level": 1, "price": 102.0, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 104.0, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 106.0, "allocation": 0.2, "r_multiple": 3.0},
]


def _outcome(*, status, exit_price, tps_hit, n=0, entry=100.0, sl=98.0, strategy="MOMENTUM"):
    """A row as the buggy timeout path would have written it: the whole
    position priced at the timeout price."""
    start = datetime.now(timezone.utc) - timedelta(hours=10) + timedelta(minutes=5 * n)
    pnl = (exit_price - entry) / entry * 100
    return Signal(
        id=f"s{n}", symbol=f"T{n}", signal_type="SCREENER", direction="LONG",
        entry_price=entry, tp=106.0, sl=sl, strategy=strategy, tp_ladder=_LADDER,
        tps_hit=list(tps_hit), status=status.value, exit_price=exit_price,
        pnl_pct=round(pnl, 3), r_multiple=round(pnl / 2.0, 3),
        activated_at=start.isoformat(),
        exit_time=(start + timedelta(hours=1)).isoformat(),
        resolved_at=(start + timedelta(hours=1)).isoformat(),
    ).to_dict()


def test_a_timeout_that_banked_tp1_is_rebooked_upward(store):
    """Drifted back toward entry: the slice sold at TP1 was forgotten, so the
    trade was recorded at a fraction of what it made."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=100.5, tps_hit=[1])])
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["rebooked"] == 1
    row = store.read(OUTCOMES_KEY)[0]
    # 50% at +2.0% (TP1) + 50% at +0.5% = +1.25%
    assert row["pnl_pct"] == 1.25
    assert row["r_multiple"] == 0.625


def test_a_timeout_parked_above_the_rung_is_rebooked_downward(store):
    """The error runs both ways: nothing was credited as sold at TP1, so a
    trade parked above it was booked as if the whole position rode to the end."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=103.0, tps_hit=[1])])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    row = store.read(OUTCOMES_KEY)[0]
    assert row["pnl_pct"] == 2.5          # was 3.0
    assert row["r_multiple"] == 1.25


def test_a_flat_expiry_can_become_a_graded_win(store):
    """The worst case. Blended, the trade cleared the dead band — but it was
    filed as having said nothing, which keeps it out of the win rate and out of
    the paper balance entirely."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_FLAT,
                                        exit_price=100.2, tps_hit=[1])])
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["status_changed"] == 1
    row = store.read(OUTCOMES_KEY)[0]
    assert row["status"] == Status.EXPIRED_WIN.value
    assert row["pnl_pct"] == 1.1          # 50% at +2.0% + 50% at +0.2%
    assert Status(row["status"]).is_graded


def test_rows_with_no_banked_rung_are_left_alone(store):
    """Nothing was sold early, so the single-exit price is already correct."""
    before = [_outcome(status=Status.EXPIRED_LOSS, exit_price=99.0, tps_hit=[])]
    store.write(OUTCOMES_KEY, before)
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["rebooked"] == 0
    assert store.read(OUTCOMES_KEY) == before


def test_blended_paths_are_not_touched(store):
    """A TP_HIT row already carries a blended PnL and a synthetic exit price
    derived from it. Re-blending that price would book the trade twice."""
    row = _outcome(status=Status.TP_HIT, exit_price=101.0, tps_hit=[1])
    store.write(OUTCOMES_KEY, [row])
    report = rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert report["rebooked"] == 0
    assert store.read(OUTCOMES_KEY)[0]["pnl_pct"] == row["pnl_pct"]


def test_a_dry_run_writes_nothing(store):
    """A ledger is not something to rewrite before looking at what would move."""
    before = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1])]
    store.write(OUTCOMES_KEY, list(before))
    report = rebank_outcomes(store, TrackerSettings(), dry_run=True)

    assert report["rebooked"] == 1 and report["dry_run"] is True
    assert store.read(OUTCOMES_KEY) == before


def test_rebanking_twice_changes_nothing_the_second_time(store):
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=100.5, tps_hit=[1], n=i)
                               for i in range(3)])
    assert rebank_outcomes(store, TrackerSettings(), dry_run=False)["rebooked"] == 3
    assert rebank_outcomes(store, TrackerSettings(), dry_run=False)["rebooked"] == 0


def test_learning_memory_is_never_touched(store):
    """It lives under its own key and holds trades seeded from backtests that
    never appear in the outcome log — rebuilding it from that log would discard
    them. Its staleness is documented instead."""
    memory = {"strategies": {"MOMENTUM": {"trades": 40, "wins": 22,
                                          "pnl_sum": 15.5, "r_sum": 7.75}},
              "symbols": {}}
    store.write(MEMORY_KEY, memory)
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_FLAT,
                                        exit_price=100.2, tps_hit=[1])])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    assert store.read(MEMORY_KEY) == memory


def test_the_balance_is_patched_by_a_ratio_not_rebuilt(store):
    """The log is capped, so a rebuild is not a smaller version of the answer.

    An account that settled 99 trades against a log holding 4 of them is the
    live shape: MAX_OUTCOMES discards the oldest rows. Replaying that log
    re-anchors the balance to whenever the survivors begin — which is how a
    -2.3R correction came back as -55%.
    """
    rows = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1], n=i)
            for i in range(4)]
    store.write(OUTCOMES_KEY, rows)
    store.write(ACCOUNT_KEY, {"balance": 9999.0, "trades": 99,
                              "realized": 0.0, "peak": 9999.0})

    report = rebank_outcomes(store, TrackerSettings(), start_balance=1000.0,
                             risk_pct=1.0, dry_run=False)
    assert report["balance_before"] == 9999.0
    # Four rows moved +0.375R each at 1% risk: about +1.5%, not -90%.
    assert 10140.0 < report["balance_after"] < 10160.0
    assert PaperAccount(store, 1000.0, 1.0).balance == report["balance_after"]
    assert PaperAccount(store, 1000.0, 1.0).summary()["trades"] == 99


def test_the_ratio_agrees_with_a_full_replay_when_the_log_is_complete(store):
    """The claim the ratio rests on, checked against the thing it replaces.

    ``apply`` moves the balance multiplicatively, so the final figure is a
    product and one changed row patches it by a ratio — exactly, and wherever
    that row sat in the sequence. If that were only approximately true the
    correction would drift, so it is asserted against the replay rather than
    argued for in a comment.
    """
    rows = [_outcome(status=Status.EXPIRED_WIN, exit_price=p, tps_hit=t, n=i)
            for i, (p, t) in enumerate([(100.5, [1]), (103.0, [1]), (104.5, [1, 2]),
                                        (100.2, [1]), (99.5, [1])])]
    store.write(OUTCOMES_KEY, rows)
    # A complete log: the account settled exactly the rows it still holds.
    account = PaperAccount(store, 1000.0, 1.0)
    for d in rows:
        account.apply(Signal.from_dict(d))

    report = rebank_outcomes(store, TrackerSettings(), start_balance=1000.0,
                             risk_pct=1.0, dry_run=False)
    assert report["rebooked"] == 5
    assert abs(report["balance_after"] - replay_balance(store, 1000.0, 1.0)) < 0.02


def test_a_replay_over_a_truncated_log_is_refused(store):
    """Named, not swallowed: the numbers it returns look entirely plausible."""
    from wolf.rebank import TruncatedLog

    store.write(OUTCOMES_KEY, [_outcome(status=Status.SL_HIT, exit_price=98.0,
                                        tps_hit=[], n=i) for i in range(3)])
    store.write(ACCOUNT_KEY, {"balance": 2562.58, "trades": 500,
                              "realized": 0.0, "peak": 2562.58})
    with pytest.raises(TruncatedLog):
        replay_balance(store, 1000.0, 1.0)
    assert PaperAccount(store, 1000.0, 1.0).balance == 2562.58   # left alone


def test_replaying_an_empty_log_returns_the_starting_balance(store):
    assert replay_balance(store, start_balance=1000.0) == 1000.0


def test_the_repair_refuses_when_the_rows_it_needs_are_gone(store):
    """Asked for more corrected rows than survive, it refuses rather than
    correcting by whatever is left — which would be a plausible wrong number,
    the exact failure it exists to undo."""
    from wolf.rebank import repair_balance

    rows = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1], n=i)
            for i in range(3)]
    store.write(OUTCOMES_KEY, rows)
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    bad = repair_balance(store, 2562.58, count=5, dry_run=True)
    assert "error" in bad and bad["matched"] == 3

    good = repair_balance(store, 2562.58, count=3, dry_run=False)
    assert "error" not in good and good["matched"] == 3
    # Each row was booked 0.375R too low, so the repair lifts the balance.
    assert 2585.0 < good["balance_after"] < 2600.0
    assert PaperAccount(store, 1000.0, 1.0).balance == good["balance_after"]


def test_the_real_timeout_price_survives_the_rewrite(store):
    """``exit_price`` becomes a synthetic figure consistent with the blended
    PnL, which destroys the one input the blend reads. The market price is kept
    beside it, and that — not a marker — is what makes a second run a no-op."""
    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN,
                                        exit_price=100.5, tps_hit=[1])])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    row = store.read(OUTCOMES_KEY)[0]
    assert row["timeout_price"] == 100.5
    assert row["exit_price"] != 100.5   # synthetic, consistent with +1.25%
    assert round(row["exit_price"], 6) == 101.25


# ── the two ways an operator actually reaches it ──────────────────────────
def _router_app(store):
    from types import SimpleNamespace

    from wolf.config import Settings

    return SimpleNamespace(analyze=None, store=store, settings=Settings())


def test_the_command_reports_without_writing_until_told_to(store):
    """The API is not exposed on this deployment; Telegram is the way in."""
    from wolf.notify.commands import CommandRouter

    before = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1])]
    store.write(OUTCOMES_KEY, list(before))
    router = CommandRouter(_router_app(store))

    reply = router.handle("/rebank")
    assert "dry run" in reply and "1 re-booked" in reply
    assert store.read(OUTCOMES_KEY) == before
    assert "confirm" in reply

    assert "written" in router.handle("/rebank confirm")
    assert store.read(OUTCOMES_KEY)[0]["pnl_pct"] == 1.25


def test_a_mistyped_argument_does_not_write(store):
    """The only command here that writes, reached from a phone keyboard.

    Anything that is not the word ``confirm`` has to be a usage error rather
    than a silent dry run, or a fat-fingered ``/rebank confrim`` would read as
    having done the job it did not do.
    """
    from wolf.notify.commands import CommandRouter

    before = [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1])]
    store.write(OUTCOMES_KEY, list(before))

    reply = CommandRouter(_router_app(store)).handle("/rebank confrim")
    assert "Usage" in reply
    assert store.read(OUTCOMES_KEY) == before


def test_the_command_is_listed_in_help(store):
    from wolf.notify.commands import CommandRouter

    assert "/rebank" in CommandRouter(_router_app(store)).handle("/help")


def test_the_cli_runs_against_the_state_dir_it_prints(tmp_path, monkeypatch, capsys):
    """Running this in the wrong place is the failure mode that matters.

    STATE_DIR defaults to a relative path, so the same command started outside
    the container rewrites an empty ledger, reports "0 scanned" and reads as a
    clean bill of health. Printing the resolved path is what makes that
    visible, so it is asserted rather than assumed.
    """
    from wolf.rebank import main
    from wolf.state import StateStore

    state_dir = tmp_path / "live"
    StateStore(str(state_dir)).write(
        OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5, tps_hit=[1])]
    )
    monkeypatch.setenv("STATE_DIR", str(state_dir))

    assert main([]) == 0
    out = capsys.readouterr().out
    assert str(state_dir) in out
    assert "dry run" in out and "--confirm" in out
    assert StateStore(str(state_dir)).read(OUTCOMES_KEY)[0]["pnl_pct"] != 1.25

    assert main(["--confirm"]) == 0
    assert "written" in capsys.readouterr().out
    assert StateStore(str(state_dir)).read(OUTCOMES_KEY)[0]["pnl_pct"] == 1.25


def test_a_run_with_nothing_to_do_says_so_rather_than_printing_a_delta(store):
    from wolf.rebank import rebank_outcomes, render

    store.write(OUTCOMES_KEY, [_outcome(status=Status.SL_HIT, exit_price=98.0, tps_hit=[])])
    out = render(rebank_outcomes(store, TrackerSettings(), dry_run=True))
    assert "already agrees with the blend" in out
    assert "r_delta" not in out


def test_the_repair_is_reachable_from_the_chat(store):
    """The CLI needs a machine the owner may not be near, and the figures the
    repair takes are already in the chat — they come from the backfill's own
    report, which is where it was read."""
    from wolf.notify.commands import CommandRouter

    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5,
                                        tps_hit=[1], n=i) for i in range(3)])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)
    router = CommandRouter(_router_app(store))

    assert "Usage" in router.handle("/rebank repair")
    assert "Usage" in router.handle("/rebank repair 2562.58")
    assert "Usage" in router.handle("/rebank repair banyak 36")

    dry = router.handle("/rebank repair 2562.58 3")
    assert "dry run" in dry and "2,591" in dry
    assert PaperAccount(store, 1000.0, 1.0).balance != 2591.44

    assert "written" in router.handle("/rebank repair 2562.58 3 confirm")
    assert PaperAccount(store, 1000.0, 1.0).balance == 2591.44


def test_the_repair_from_the_chat_refuses_rather_than_guessing(store):
    """Asked for rows that are no longer there, it says so instead of
    correcting by whatever survived."""
    from wolf.notify.commands import CommandRouter

    store.write(OUTCOMES_KEY, [_outcome(status=Status.EXPIRED_WIN, exit_price=100.5,
                                        tps_hit=[1], n=i) for i in range(2)])
    rebank_outcomes(store, TrackerSettings(), dry_run=False)

    reply = CommandRouter(_router_app(store)).handle("/rebank repair 2562.58 36 confirm")
    assert "refused" in reply
    assert PaperAccount(store, 1000.0, 1.0).balance != 2562.58


def test_the_estimate_needs_no_rows_and_says_it_is_one(store):
    """Once the corrected rows rotate out, the exact repair can only refuse —
    while the balance is still wrong by half. Both figures the estimate takes
    are in the backfill's report, so it needs nothing from the log."""
    from wolf.rebank import render_repair, repair_balance_from_delta

    store.write(ACCOUNT_KEY, {"balance": 1162.36, "trades": 500,
                              "realized": 162.36, "peak": 2600.0})
    rep = repair_balance_from_delta(store, 2562.58, -2.303, dry_run=False)

    assert 2500.0 < rep["balance_after"] < 2508.0
    assert PaperAccount(store, 1000.0, 1.0).balance == rep["balance_after"]
    assert "estimate" in render_repair(rep)


def test_the_estimate_matches_the_exact_product_on_the_rows_the_report_printed():
    """The shortcut's accuracy is measured, not asserted in a docstring: on
    the fifteen rows the live report printed, exp(k * sum(dR)) and the exact
    product of ratios differ by 0.0258%."""
    import math

    k = 0.01
    rows = [(1.043, 1.309), (0.697, 0.848), (0.953, 0.976), (2.387, 1.577),
            (1.752, 1.451), (1.508, 1.402), (1.738, 1.447), (0.887, 0.943),
            (0.343, 0.672), (1.672, 1.435), (1.736, 1.368), (0.565, 1.213),
            (0.641, 1.228), (1.484, 1.397), (1.871, 1.474)]
    exact = math.prod((1 + k * n) / (1 + k * o) for o, n in rows)
    shortcut = math.exp(k * sum(n - o for o, n in rows))
    assert abs(exact - shortcut) / exact < 0.001


def test_a_refused_repair_points_at_the_estimate(store):
    """A refusal with no way forward is a dead end on a phone."""
    from wolf.notify.commands import CommandRouter

    reply = CommandRouter(_router_app(store)).handle("/rebank repair 2562.58 36")
    assert "refused" in reply and "-2.303" in reply


def test_the_chat_tells_a_count_from_an_r_delta(store):
    from wolf.notify.commands import CommandRouter

    store.write(ACCOUNT_KEY, {"balance": 1162.36, "trades": 500,
                              "realized": 162.36, "peak": 2600.0})
    router = CommandRouter(_router_app(store))
    assert "estimate" in router.handle("/rebank repair 2562.58 -2.303")
    assert "estimate" in router.handle("/rebank repair 2562.58 -2.303r")
    assert "estimate" not in router.handle("/rebank repair 2562.58 36")
    assert "2,504" in router.handle("/rebank repair 2,562.58 -2.303 confirm")
    assert 2500.0 < PaperAccount(store, 1000.0, 1.0).balance < 2508.0
