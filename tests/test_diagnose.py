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
