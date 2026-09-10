"""Tests for grading what the chase gate threw away.

The gate drops a market entry that has already run past its detector's quote,
and it does so before ``record_signal`` — so its output reaches no count, no
bucket and no strategy row. Recording the drops made it visible; these cover
the half that makes it *judged*, and the shape of each test is the sentence it
proves about how a drop is reconstituted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.chase_audit import CHASE_DROPS_KEY, _as_signal, grade_chase_drops, render
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
    assert report["skipped_unbuildable"] == 1
    assert report["skipped_nohistory"] == 1
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
