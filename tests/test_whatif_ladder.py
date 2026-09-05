"""Tests for re-cutting the target ladder on an already-resolved sample."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wolf.config import LadderSettings, TrackerSettings
from wolf.models import Candle, Signal, Status
from wolf.tracker import OUTCOMES_KEY, Tracker
from wolf.whatif import (
    LADDER_VARIANTS, LadderVariant, compare_ladder_geometry, render_ladder,
)

LIVE = LADDER_VARIANTS[0]

# entry 100 / stop 95, so 1R is 5 points and every rung is a multiple of it.
_ENTRY, _SL = 100.0, 95.0
_LIVE_RUNGS = [
    {"level": 1, "price": 105.0, "allocation": 0.5, "r_multiple": 1.0},
    {"level": 2, "price": 110.0, "allocation": 0.3, "r_multiple": 2.0},
    {"level": 3, "price": 115.0, "allocation": 0.2, "r_multiple": 3.0},
]


def _signal(**over) -> Signal:
    base = dict(
        symbol="BTCUSDT", signal_type="BREAKOUT", direction="LONG",
        entry_price=_ENTRY, tp=115.0, sl=_SL,
        strategy="MOMENTUM", timeframe="15m",
        entry_mode="MOMENTUM_NOW", tp_ladder=list(_LIVE_RUNGS),
        status=Status.TP_HIT.value, activated=True,
    )
    base.update(over)
    return Signal(**base)


# ── geometry arithmetic ─────────────────────────────────────────────────────


def test_a_variant_places_its_rungs_at_multiples_of_the_risk_unit():
    """The geometry is expressed in R, so the prices follow from entry and stop.

    This is what makes the comparison fair: the risk unit is identical across
    variants, so every column's R means the same thing.
    """
    rungs = LIVE.rungs_for(_signal())
    assert [r["price"] for r in rungs] == [105.0, 110.0, 115.0]
    assert [r["allocation"] for r in rungs] == [0.5, 0.3, 0.2]

    tighter = LadderVariant("rr2.0", 2.0, (1 / 3, 2 / 3, 1.0), (0.5, 0.3, 0.2))
    # A 2.0R ladder on a 5-point risk unit: 2/3R, 4/3R and 2R above entry.
    assert [round(r["price"], 4) for r in tighter.rungs_for(_signal())] == [
        103.3333, 106.6667, 110.0
    ]


def test_a_short_places_its_rungs_below_entry():
    """Direction is read from the signal, not assumed."""
    short = _signal(direction="SHORT", entry_price=100.0, sl=105.0, tp=85.0)
    assert [r["price"] for r in LIVE.rungs_for(short)] == [95.0, 90.0, 85.0]


def test_the_live_variant_reproduces_the_configured_ceiling():
    """``live`` must be the geometry actually in force, or the base column lies."""
    assert LIVE.settings(LadderSettings()).full_run_r == LadderSettings().full_run_r


def test_a_variant_inherits_the_live_stop_rule():
    """Only the targets are under test; the stop rule is not silently re-run."""
    live = LadderSettings(stop_advance="ladder")
    assert LADDER_VARIANTS[3].settings(live).stop_advance == "ladder"


# ── replay ──────────────────────────────────────────────────────────────────


def _seed(store, fake_client, path, symbol="BTCUSDT"):
    """Book one resolved outcome and give the replay a history to fetch.

    The outcome is written straight to the store rather than graded through
    the tracker: these tests are about how the *replay* re-cuts a ladder, and
    driving the live grader first would tie them to whatever the live geometry
    happens to do with the same candles.
    """
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    sig = _signal(created_at=created.isoformat(), activated_at=created.isoformat())
    store.write(OUTCOMES_KEY, [sig.to_dict()])

    start_ms = int(created.timestamp() * 1000)
    fake_client.klines[symbol] = [
        # One bar before the signal, so the history demonstrably reaches back
        # to the entry — which is what the replay checks for.
        Candle(time=start_ms - 900_000, open=100, high=100, low=100, close=100, volume=1.0)
    ] + [
        Candle(time=start_ms + (i + 1) * 900_000, open=o, high=h, low=l, close=c, volume=1.0)
        for i, (o, h, l, c) in enumerate(path)
    ]
    return Tracker(store, fake_client, TrackerSettings(), ladder=LadderSettings())


# Runs to 111 and stalls: clears a 2.0R ladder outright, never reaches the live
# ladder's 115 third rung, never falls back to the breakeven stop. Exactly the
# shape the whole exercise is about.
_STALLS_BELOW_TP3 = [
    (100, 106, 100, 105),
    (105, 111, 104, 110),
    (110, 111, 110, 111),
    (111, 111, 110, 111),
]


def test_every_variant_is_scored_on_the_same_trade(store, fake_client):
    """Columns are paired, so a variant cannot win on a different sample."""
    report = compare_ladder_geometry(_seed(store, fake_client, _STALLS_BELOW_TP3))

    assert not report["error"]
    assert [r.label for r in report["results"]][0] == "live"
    assert {r.n for r in report["results"]} == {report["scored"]}
    assert report["scored"] == 1


def test_the_ceiling_column_matches_each_geometry(store, fake_client):
    """``run`` is the R a perfect trade banks, not the advertised ratio.

    The live 1:3 ladder banks 1.7R when every rung fills, and quoting 3.0 here
    is precisely the error that lets a system be sold at 1:3 while realising
    1:1.
    """
    report = compare_ladder_geometry(_seed(store, fake_client, _STALLS_BELOW_TP3))
    ceilings = {r.label: r.full_run_r for r in report["results"]}
    assert ceilings["live"] == 1.7      # .5x1R + .3x2R + .2x3R
    assert ceilings["2rung"] == 1.5     # .5x1R + .5x2R
    assert ceilings["even"] == 2.0      # (1R + 2R + 3R) / 3
    assert ceilings["backload"] == 2.1  # .3x1R + .3x2R + .4x3R


def test_an_unsettled_trade_is_counted_not_hidden(store, fake_client):
    """A geometry the history never resolved must say so.

    A wider ladder takes longer to fill, so it leaves more trades open, and
    those are marked to the last close rather than to a rung. Without the
    count a variant could win purely on unsettled positions — a statement
    about the history's length, not about the geometry.
    """
    report = compare_ladder_geometry(_seed(store, fake_client, _STALLS_BELOW_TP3))
    rows = {r.label: r for r in report["results"]}
    # 111 clears every rung of the 2.0R ladder, so that column settles.
    assert rows["rr2.0"].unresolved == 0
    # The live ladder's last rung sits at 115 and price stopped at 111, so the
    # position is still open when the candles run out.
    assert rows["live"].unresolved == 1


def test_the_wider_ladder_wins_only_on_the_slice_it_never_closed(store, fake_client):
    """The exact bias the ``unresolved`` column exists to expose.

    Price stalls at 111. The 2.0R ladder fills every rung and settles at
    1.133R — a number the candles actually paid. The live ladder banks TP1 and
    TP2 for 1.1R and is still holding its last 20% when the history runs out,
    so that slice is marked at the closing 111 (+2.2R) and the column reports
    1.54R. Live "wins" by 0.4R that was never realised, and would evaporate if
    price came back.

    This is why the winner is not read off the mean alone. A comparison that
    printed only meanR would recommend keeping a ladder on the strength of an
    open position.
    """
    report = compare_ladder_geometry(_seed(store, fake_client, _STALLS_BELOW_TP3))
    rows = {r.label: r for r in report["results"]}

    # Every rung of the 2.0R ladder fills: .5x(2/3)R + .3x(4/3)R + .2x2R.
    assert rows["rr2.0"].mean_r == 1.133
    assert rows["rr2.0"].unresolved == 0

    # Live: .5x1R + .3x2R banked, then .2 of the position carried at +2.2R.
    assert rows["live"].mean_r == 1.54
    assert rows["live"].unresolved == 1

    # The apparent edge is entirely the unsettled slice, and the card says so.
    assert rows["live"].mean_r > rows["rr2.0"].mean_r
    assert "untested" in render_ladder(report)


# ── reporting ───────────────────────────────────────────────────────────────


def test_the_render_warns_when_the_winner_is_the_least_settled(store, fake_client):
    """The caveat has to sit next to the number, not in a docstring."""
    report = compare_ladder_geometry(_seed(store, fake_client, _STALLS_BELOW_TP3))
    # Force the flattered-winner branch: a variant that wins while leaving more
    # trades open is reported as untested, never as a recommendation.
    report["results"][1].mean_r = report["results"][0].mean_r + 1.0
    report["results"][1].unresolved = report["results"][0].unresolved + 3
    out = render_ladder(report)
    assert "untested" in out
    assert "3 more trade(s) unsettled" in out


def test_the_render_names_the_live_geometry_as_the_baseline(store, fake_client):
    """A gain is quoted against the rule in force, not the worst of the set."""
    report = compare_ladder_geometry(_seed(store, fake_client, _STALLS_BELOW_TP3))
    out = render_ladder(report)
    assert "live" in out
    assert "read the shape across variants" in out


def test_render_reports_the_error_rather_than_an_empty_table():
    assert "no graded signals" in render_ladder(
        {"error": "no graded signals with a usable risk unit", "results": []}
    )
