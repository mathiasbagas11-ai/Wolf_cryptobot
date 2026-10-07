"""Tests for grading what the chase gate threw away.

The gate drops a market entry that has already run past its detector's quote,
and it does so before ``record_signal`` — so its output reaches no count, no
bucket and no strategy row. Recording the drops made it visible; these cover
the half that makes it *judged*, and the shape of each test is the sentence it
proves about how a drop is reconstituted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.chase_audit import (
    CHASE_DROPS_KEY, _as_signal, grade_chase_drops, grade_pending_drops, render,
)
from wolf.config import LadderSettings, TrackerSettings
from wolf.models import Candle, EntryMode, Signal, Status
from wolf.tracker import OUTCOMES_KEY, Tracker

_BAR_MS = 900_000
_LADDER = LadderSettings()


class _PathClient:
    """Serves 15m candles counted back from now, as the kline API does."""

    def __init__(self, paths: dict) -> None:
        self.paths = paths

    def get_klines(self, symbol, interval="15m", limit=100):
        closes = self.paths.get(symbol)
        if not closes:
            return []
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        start = now - len(closes) * _BAR_MS
        out = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            out.append(Candle(time=start + i * _BAR_MS, open=o,
                              high=max(o, c) * 1.001, low=min(o, c) * 0.999,
                              close=c, volume=1000.0))
        return out[-limit:]


def _drop(symbol="SOLUSDT", strategy="SCALP", live=100.0, sl=97.5,
          chase_r=0.62, hours_ago=8.0, **extra) -> dict:
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    row = {
        "at": at.isoformat(timespec="seconds"), "symbol": symbol,
        "strategy": strategy, "signal_type": strategy, "timeframe": "15m",
        "direction": "LONG", "score": 78, "quoted": live * 0.99,
        "live": live, "sl": sl, "chase_r": chase_r, "limit": 0.5,
    }
    row.update(extra)
    return row


def _rising(n=60, start=100.0, step=0.35):
    return [start + i * step for i in range(n)]


def _falling(n=60, start=100.0, step=0.30):
    return [start - i * step for i in range(n)]


def _tracker(store, paths=None) -> Tracker:
    return Tracker(store, _PathClient(paths or {}), TrackerSettings())


# ── Reconstitution ───────────────────────────────────────────────────────

def test_a_drop_is_rebuilt_as_the_signal_the_requote_would_have_written():
    """A re-quote moves the entry and nothing else. The stop marks a level in
    the market, so it stays; the ladder is rebuilt on the stretched risk unit
    at the R multiples the policy sets."""
    sig = _as_signal(_drop(live=100.0, sl=97.5), _LADDER)
    assert sig is not None
    assert sig.entry_price == 100.0          # the live price, not the quote
    assert sig.sl == 97.5                    # unmoved
    assert sig.entry_mode == EntryMode.MOMENTUM_NOW.value
    assert sig.tp_ladder and sig.tp == sig.tp_ladder[-1]["price"]
    # Every rung sits at its R multiple off the *new*, wider risk unit.
    risk = sig.entry_price - sig.sl
    for rung in sig.tp_ladder:
        assert abs((rung["price"] - sig.entry_price) / risk - rung["r_multiple"]) < 1e-6


def test_a_rebuilt_drop_is_priced_at_the_drop_instant_not_a_bar_close():
    """`entry_quoted_live` is load-bearing: the entry is the live price read at
    that moment, so the replay must open strictly after it. Left False, the
    window back to the last bar close would be credited to the entry and hidden
    from the stop — on exactly the fast moves the gate fires on."""
    sig = _as_signal(_drop(), _LADDER)
    assert sig is not None and sig.entry_quoted_live is True
    assert sig.priced_at is None


def test_momentum_drops_recover_the_signal_type_the_timeout_keys_off():
    """Rows written before the type was recorded still have to grade under the
    right timeout, and MOMENTUM's signal_type is SCREENER, not its name."""
    legacy = _drop(strategy="MOMENTUM")
    del legacy["signal_type"]
    sig = _as_signal(legacy, _LADDER)
    assert sig is not None and sig.signal_type == "SCREENER"
    assert TrackerSettings().timeout_for(sig.signal_type) == 48


def test_a_drop_whose_stop_is_already_through_is_not_graded():
    """Guessing at an inverted geometry would be evidence either way, and it is
    neither."""
    assert _as_signal(_drop(live=100.0, sl=101.0), _LADDER) is None
    assert _as_signal(_drop(live=0.0), _LADDER) is None
    assert _as_signal({"symbol": "X"}, _LADDER) is None


# ── Grading ──────────────────────────────────────────────────────────────

def test_drops_are_graded_over_the_candles_that_actually_followed(store):
    store.write(CHASE_DROPS_KEY, [
        _drop(symbol="WINNER"), _drop(symbol="LOSER"),
    ])
    tracker = _tracker(store, {"WINNER": _rising(), "LOSER": _falling()})
    report = grade_chase_drops(tracker)

    assert report["error"] == ""
    assert report["overall"]["n"] == 2
    by_symbol = {s["symbol"]: s for s in report["scored"]}
    assert by_symbol["WINNER"]["r"] > 0
    assert by_symbol["LOSER"]["r"] < 0


def test_an_ungradeable_drop_is_counted_not_hidden(store):
    """A skipped row is a gap in the answer, so it has to be visible next to
    it — otherwise the sample silently shrinks toward whatever was gradeable."""
    store.write(CHASE_DROPS_KEY, [
        _drop(symbol="WINNER"),
        _drop(symbol="WINNER", sl=101.0),        # stop already through
        _drop(symbol="NOHISTORY"),               # client serves nothing
    ])
    report = grade_chase_drops(_tracker(store, {"WINNER": _rising()}))
    assert report["overall"]["n"] == 1
    assert report["skipped"]["unbuildable"] == 1
    assert report["skipped"]["no_data"] == 1
    assert report["skipped"]["too_old"] == 0
    assert "skipped" in render(report)


def test_unresolved_drops_are_reported_separately(store):
    """A drop still open when the history runs out is marked to the last close,
    which is a guess about a position that was never closed. The gate fires
    hardest on fast moves, so these are not a rounding error."""
    store.write(CHASE_DROPS_KEY, [_drop(symbol="SLOW", hours_ago=0.5)])
    # Barely any history and a drift too small to reach a rung or the stop.
    report = grade_chase_drops(_tracker(store, {"SLOW": [100.0, 100.01, 100.02]}))
    if report["overall"]["n"]:
        assert report["overall"]["resolved"] < report["overall"]["n"]
        assert "still open" in render(report)


def test_the_report_splits_on_how_far_past_the_quote_each_drop_ran(store):
    """The half that can argue the limit without a second sample. If drops that
    ran far return what near ones do, the limit's exact value is not what
    matters; if they do not, the drops say where to put it."""
    store.write(CHASE_DROPS_KEY, [
        _drop(symbol="WINNER", chase_r=0.55), _drop(symbol="WINNER", chase_r=0.60),
        _drop(symbol="LOSER", chase_r=1.20), _drop(symbol="LOSER", chase_r=1.40),
    ])
    report = grade_chase_drops(
        _tracker(store, {"WINNER": _rising(), "LOSER": _falling()})
    )
    dist = report["by_distance"]
    assert dist["near"]["n"] == 2 and dist["far"]["n"] == 2
    assert dist["near"]["mean_r"] > dist["far"]["mean_r"]
    assert "distance" in render(report)


def test_the_comparison_against_taken_trades_is_charged_as_unpaired(store):
    """Drops and taken trades are different setups, so this is a level question
    and has to be priced as one — Welch, discounted by the overlap the live
    book was running. It should say nothing at these sizes, and the card must
    let it."""
    rows = []
    # Varied outcomes on purpose: mean_gap refuses a zero-variance population,
    # correctly — a spread of exactly nothing has no standard error to divide
    # by — and a fixture where every trade returns the same R would be testing
    # that refusal rather than the discount.
    for i, r in enumerate((-1.0, -0.6, +1.4, -0.9)):
        start = datetime.now(timezone.utc) - timedelta(hours=6) + timedelta(minutes=20 * i)
        rows.append(Signal(
            symbol=f"T{i}", signal_type="SCALP", direction="LONG",
            entry_price=100, tp=103, sl=98, strategy="SCALP",
            tp_ladder=[{"level": 1, "price": 101}],
            status=(Status.TP_HIT if r > 0 else Status.SL_HIT).value,
            pnl_pct=r * 2.0, r_multiple=r, activated_at=start.isoformat(),
            exit_time=(start + timedelta(hours=1)).isoformat(),
            resolved_at=(start + timedelta(hours=1)).isoformat(),
        ).to_dict())
    store.write(OUTCOMES_KEY, rows)
    store.write(CHASE_DROPS_KEY, [
        _drop(symbol="WINNER"), _drop(symbol="LOSER"), _drop(symbol="WINNER"),
    ])

    report = grade_chase_drops(
        _tracker(store, {"WINNER": _rising(), "LOSER": _falling()}), overlap=4.0
    )
    gap = report["gap"]
    assert gap is not None and report["taken_n"] == 4
    # The discount is charged, not skipped: the honest t is below the nominal.
    assert abs(gap["t"]) < abs(gap["t_nominal"])
    card = render(report)
    assert "unpaired" in card and "level question" in card


def test_nothing_recorded_says_so_rather_than_reporting_zero(store):
    report = grade_chase_drops(_tracker(store))
    assert report["scored"] == []
    assert "no chase drops recorded" in render(report)


# ── Scheduled grading ────────────────────────────────────────────────────
#
# The audit used to replay on demand and the first live run lost 21 of 48
# drops, all to "no history reaching back". That is structural, not bad luck:
# the replay asks for candles from now back to the drop, so the bars required
# grow every hour and skips correlate with age. Grading near the event and
# storing the verdict is the fix; these cover it.

def test_a_drop_is_graded_once_and_the_verdict_is_stored(store):
    store.write(CHASE_DROPS_KEY, [_drop(symbol="WINNER", hours_ago=12.0)])
    tracker = _tracker(store, {"WINNER": _rising()})

    result = grade_pending_drops(tracker)
    assert result["graded"] == 1

    row = store.read(CHASE_DROPS_KEY)[0]
    assert row["graded"]["r"] > 0
    assert row["graded"]["resolved"] is True

    # Re-running does not re-grade: the verdict is the record, not a cache.
    assert grade_pending_drops(tracker)["graded"] == 0


def test_a_drop_too_young_to_have_settled_is_left_alone(store):
    """The floor is the strategy's own timeout, so the replay has had as long
    as the trade would have. SCALP times out at 10h."""
    store.write(CHASE_DROPS_KEY, [_drop(symbol="WINNER", hours_ago=2.0)])
    assert grade_pending_drops(_tracker(store, {"WINNER": _rising()}))["graded"] == 0
    assert "graded" not in store.read(CHASE_DROPS_KEY)[0]


def test_a_drop_past_the_ceiling_is_never_graded_late(store):
    """Beyond the ceiling the replay cannot be trusted to reach back, and an
    answer computed from the wrong prices is worse than no answer."""
    store.write(CHASE_DROPS_KEY, [_drop(symbol="WINNER", hours_ago=200.0)])
    tracker = _tracker(store, {"WINNER": _rising()})
    result = grade_pending_drops(tracker)
    assert result["graded"] == 0 and result["expired"] == 1


def test_a_transient_fetch_failure_leaves_the_drop_pending(store):
    """A venue that answers nothing this hour may answer the next. Recording a
    verdict would spend the drop's one chance on a network blip."""
    store.write(CHASE_DROPS_KEY, [_drop(symbol="LATER", hours_ago=12.0)])
    tracker = _tracker(store, {})                      # serves nothing
    assert grade_pending_drops(tracker)["failed"] == 1
    assert "graded" not in store.read(CHASE_DROPS_KEY)[0]

    tracker = _tracker(store, {"LATER": _rising()})    # venue comes back
    assert grade_pending_drops(tracker)["graded"] == 1


def test_an_unbuildable_drop_is_recorded_rather_than_retried_forever(store):
    """Unbuildable is permanent — an inverted geometry does not heal — so it is
    settled once instead of costing a fetch every hour until it ages out."""
    store.write(CHASE_DROPS_KEY, [_drop(symbol="WINNER", sl=101.0, hours_ago=12.0)])
    tracker = _tracker(store, {"WINNER": _rising()})
    grade_pending_drops(tracker)
    assert store.read(CHASE_DROPS_KEY)[0]["graded"]["error"] == "unbuildable"
    assert grade_pending_drops(tracker)["considered"] == 0


def test_the_audit_prefers_a_stored_verdict_over_a_fresh_replay(store):
    """The stored verdict was taken when the history still reached. Refetching
    would answer the same question from a worse vantage point, and on old drops
    would not answer it at all."""
    # The path has to span the drop's age, or the grader fails for the very
    # reason this test is about — 20h needs 80 bars of 15m, not 60.
    store.write(CHASE_DROPS_KEY, [_drop(symbol="GONE", hours_ago=20.0)])
    assert grade_pending_drops(_tracker(store, {"GONE": _rising(n=120)}))["graded"] == 1

    # The venue now serves nothing for that symbol; the answer survives anyway.
    report = grade_chase_drops(_tracker(store, {}))
    assert report["overall"]["n"] == 1
    assert report["from_store"] == 1
    assert sum(report["skipped"].values()) == 0
    assert "graded near the event" in render(report)


def test_the_skip_breakdown_names_the_fault(store):
    """One number covering three faults with three different remedies is what
    let a 44% loss read as incidental."""
    store.write(CHASE_DROPS_KEY, [
        _drop(symbol="WINNER", hours_ago=12.0),
        _drop(symbol="WINNER", hours_ago=300.0),          # past the ceiling
        _drop(symbol="NOSERVE", hours_ago=12.0),          # venue serves nothing
        _drop(symbol="WINNER", sl=101.0, hours_ago=12.0),  # cannot be rebuilt
    ])
    report = grade_chase_drops(_tracker(store, {"WINNER": _rising()}))
    assert report["skipped"] == {"too_old": 1, "no_data": 1, "unbuildable": 1}
    card = render(report)
    assert "aged out" in card and "no candles served" in card


def test_the_grading_job_is_scheduled():
    """A measurement job whose schedule is the point: it has to run while the
    drops it grades are still reachable."""
    from wolf.chase_audit import is_gradeable

    settings = TrackerSettings()
    now = datetime.now(timezone.utc)
    # SCALP times out at 10h, the ceiling is 60h.
    assert not is_gradeable(_drop(hours_ago=2.0), settings, now)
    assert is_gradeable(_drop(hours_ago=12.0), settings, now)
    assert not is_gradeable(_drop(hours_ago=80.0), settings, now)
    # MOMENTUM times out at 48h, inside the ceiling with slack to spare.
    momentum = _drop(strategy="MOMENTUM", signal_type="SCREENER", hours_ago=50.0)
    assert is_gradeable(momentum, settings, now)
