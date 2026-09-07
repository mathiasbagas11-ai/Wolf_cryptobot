"""Tests for the adaptive learning engine, backtest simulator and /analyze."""

from __future__ import annotations

from types import SimpleNamespace

from wolf.analyze import AnalyzeService, normalize_symbol
from wolf.backtest import simulate
from wolf.config import LearningSettings
from wolf.detectors import default_detectors
from wolf.detectors.base import SignalCandidate
from wolf.learning import LearningEngine
from wolf.models import Candle, Signal
from wolf.notify.commands import CommandRouter


def _resolved(symbol="BTCUSDT", strategy="MOMENTUM", status="TP_HIT", pnl=5.0, entry=100.0, sl=95.0):
    return Signal(symbol=symbol, signal_type="SCREENER", direction="LONG",
                  entry_price=entry, tp=110, sl=sl, strategy=strategy,
                  status=status, activated=True, pnl_pct=pnl,
                  resolved_at="2026-01-01T00:00:00+00:00")


# ── learning ─────────────────────────────────────────────────────────────────
def test_learning_boosts_winner_penalises_loser(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, max_adjust=15))
    for _ in range(5):
        eng.observe(_resolved(status="TP_HIT", pnl=6.0))
    assert eng.adjustment("BTCUSDT", "MOMENTUM").delta > 0

    for _ in range(5):
        eng.observe(_resolved(symbol="ETHUSDT", status="SL_HIT", pnl=-4.0))
    assert eng.adjustment("ETHUSDT", "MOMENTUM").delta < 0


def test_learning_blacklists_bad_symbol(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3, blacklist_min_trades=8, blacklist_max_winrate=25))
    for i in range(10):
        eng.observe(_resolved(symbol="ZZZUSDT",
                              status="TP_HIT" if i < 2 else "SL_HIT",
                              pnl=6.0 if i < 2 else -4.0))
    assert eng.adjustment("ZZZUSDT", "MOMENTUM").blacklisted


def test_learning_ignores_non_graded(store):
    eng = LearningEngine(store, LearningSettings(min_samples=1))
    eng.observe(_resolved(status="INVALIDATED", pnl=0.0))
    assert eng.snapshot()["strategies"] == {}


def test_learning_seed(store):
    eng = LearningEngine(store, LearningSettings(min_samples=3))
    eng.seed([("SWING", "BTCUSDT", True, 5.0, 1.5)] * 4)
    assert eng.snapshot()["strategies"]["SWING"]["win_rate"] == 100.0


# ── backtest simulate ────────────────────────────────────────────────────────
def _future(ohlc):
    return [Candle(time=i * 900_000, open=o, high=h, low=l, close=c, volume=100.0)
            for i, (o, h, l, c) in enumerate(ohlc)]


def _cand(direction="LONG"):
    return SignalCandidate(symbol="BTCUSDT", signal_type="SCREENER", direction=direction,
                           entry_price=100.0, tp=110, sl=95, score=70, strategy="MOMENTUM",
                           reasons=["x"], entry_mode="MOMENTUM_NOW",
                           tps=[{"level": 1, "price": 105}, {"level": 2, "price": 110}])


def test_simulate_tp_win():
    sim = simulate(_cand(), _future([(100, 106, 100, 105), (105, 111, 104, 110)]))
    assert sim.status == "TP_HIT" and sim.win and sim.pnl_pct == 10.0


def test_simulate_sl_loss():
    sim = simulate(_cand(), _future([(100, 101, 94, 96)]))
    assert sim.status == "SL_HIT" and not sim.win


def test_simulate_never_activated():
    c = _cand()
    c.entry_price, c.entry_mode = 90.0, "RETEST_WAIT"
    assert simulate(c, _future([(95, 99, 92, 98)])) is None


# ── analyze + commands ───────────────────────────────────────────────────────
def _trend_candles(n=150):
    out, p = [], 100.0
    for i in range(n):
        p += 0.6
        out.append(Candle(time=i * 900_000, open=p - 0.6, high=p + 0.5, low=p - 0.8, close=p, volume=100.0))
    return out


class _Client:
    def __init__(self, candles):
        self._candles = candles

    def get_klines(self, symbol, interval="15m", limit=150):
        return list(self._candles)[-limit:]


def test_normalize_symbol():
    assert normalize_symbol("btc") == "BTCUSDT"
    assert normalize_symbol("ETH-USDT") == "ETHUSDT"
    assert normalize_symbol("") == ""


def test_analyze_card():
    out = AnalyzeService(_Client(_trend_candles()), default_detectors()).analyze("btc")
    assert "ANALYSIS · BTCUSDT" in out and "RSI" in out


def test_analyze_unknown():
    assert "Not enough data" in AnalyzeService(_Client([]), default_detectors()).analyze("zzz")


def _fake_app():
    analyze = AnalyzeService(_Client(_trend_candles()), default_detectors())
    tracker = SimpleNamespace(
        stats=lambda: {"wins": 3, "losses": 1, "win_rate": 75.0, "avg_pnl_pct": 1.2, "active": 2, "total_graded": 4},
        active_signals=lambda: [],
    )
    account = SimpleNamespace(summary=lambda: {"balance": 1050.0, "starting_balance": 1000.0, "return_pct": 5.0,
                                               "peak": 1060.0, "max_drawdown_pct": 1.0, "trades": 4, "realized": 50.0})
    learning = SimpleNamespace(snapshot=lambda: {"strategies": {"MOMENTUM": {"win_rate": 60.0, "trades": 5, "avg_r": 0.4}},
                                                 "symbols": {}, "blacklist": ["ZZZUSDT"]})
    return SimpleNamespace(analyze=analyze, tracker=tracker, account=account, learning=learning)


def test_router_commands():
    r = CommandRouter(_fake_app())
    assert "Commands" in r.handle("/help")
    assert "ANALYSIS · BTCUSDT" in r.handle("/analyze btc")
    assert "ANALYSIS · ETHUSDT" in r.handle("/eth")            # bare ticker shortcut
    assert "WR 75.0%" in r.handle("/stats")
    assert "PAPER ACCOUNT" in r.handle("/paper@WolfBot")       # @botname stripped
    assert "Blacklist" in r.handle("/learning")
    assert "Unknown" in r.handle("/wat is this")


# ── the backtest runs the bot that is actually deployed ─────────────────────


class _AlwaysFires:
    """A detector that proposes one candidate per bar with a chosen risk unit.

    Real detectors will not fire reliably on synthetic candles, and a gate test
    that silently exercises nothing is worse than no test: both sides come back
    with zero trades and the assertion passes without touching the gate.
    """

    name = "STUB"
    timeframe = "15m"

    def __init__(self, risk_pct: float) -> None:
        self._risk_pct = risk_pct

    def evaluate(self, symbol, candles, context=None, features=None):
        entry = 100.0
        return SignalCandidate(
            symbol=symbol, signal_type="SCREENER", direction="LONG",
            entry_price=entry, tp=entry * 1.05, sl=entry * (1 - self._risk_pct / 100),
            score=70, strategy="STUB", reasons=["x"], entry_mode="MOMENTUM_NOW",
            tps=[{"level": 1, "price": entry * 1.02}],
        )


def _engine(risk_pct, **kw):
    from wolf.backtest import BacktestEngine

    return BacktestEngine(
        _Client(_trend_candles(120)), [_AlwaysFires(risk_pct)],
        lookback=40, candle_limit=120, **kw
    )


def test_a_stop_too_tight_for_its_costs_is_refused_in_the_backtest_too():
    """A backtest without the live gate reports on a bot nobody runs.

    MAX_COST_R removed roughly half the live daily volume and nearly all of
    SCALP. Keeping those setups in the replay describes the strategy as it was
    before the gate existed — and the deeper the history, the more of that dead
    population it piles up, so depth without the gate makes it worse.
    """
    # 1R of 0.5% pays (20/100)/0.5 = 0.40R in costs, well over the 0.15 bar.
    gated = _engine(0.5, max_cost_r=0.15, round_trip_bps=20.0).run(["BTCUSDT"])
    assert gated["total_trades"] == 0
    assert gated["gated"] > 0


def test_an_affordable_stop_still_trades():
    """The gate has to bite on cost, not on everything."""
    # 1R of 2.0% pays 0.10R — inside the bar.
    ok = _engine(2.0, max_cost_r=0.15, round_trip_bps=20.0).run(["BTCUSDT"])
    assert ok["total_trades"] > 0
    assert ok["gated"] == 0


def test_no_gate_configured_refuses_nothing():
    """An unset MAX_COST_R must not quietly become a default bar."""
    result = _engine(0.5).run(["BTCUSDT"])
    assert result["total_trades"] > 0
    assert result["gated"] == 0


def test_the_backtest_and_the_screener_cannot_disagree_about_affordability():
    """Two copies of one rule drift, and the drift would be invisible.

    The backtest would keep reporting on a population the live path had already
    stopped taking, and nothing on either side would say so.
    """
    from wolf.screener import Screener

    engine = _engine(0.5, max_cost_r=0.15, round_trip_bps=20.0)
    for risk_pct in (0.05, 0.12, 0.5, 1.33, 1.34, 2.0, 8.0):
        cand = _AlwaysFires(risk_pct).evaluate("BTCUSDT", [])
        live_rejects = Screener._too_expensive(
            SimpleNamespace(_round_trip_bps=20.0, _max_cost_r=0.15), cand
        )
        assert engine._affordable(cand) is not live_rejects, risk_pct


def test_a_request_beyond_the_venue_cap_is_clamped_not_wished_for():
    """No venue here paginates, so asking for 5000 bars silently returns 1000.

    The extra lookback would then walk off the front of the history it was
    given, replaying bars that were never served.
    """
    from wolf.backtest import BacktestEngine

    assert BacktestEngine(_Client([]), [], candle_limit=5000).run([])["candle_limit"] == 1000


def test_the_shipped_depth_is_the_one_the_venues_can_serve():
    from wolf.config import BacktestSettings

    s = BacktestSettings()
    assert s.candle_limit == 1000
    assert s.lookback == 300


# ── learning records its judgment instead of enforcing it ───────────────────


def _benched_engine(store, hard_block: bool):
    """A learning engine holding one symbol well past the bench threshold."""
    eng = LearningEngine(store, LearningSettings(
        min_samples=3, blacklist_min_trades=8, blacklist_max_winrate=25,
        hard_block=hard_block,
    ))
    for i in range(10):
        eng.observe(_resolved(symbol="ZZZUSDT",
                              status="TP_HIT" if i < 2 else "SL_HIT",
                              pnl=6.0 if i < 2 else -4.0))
    return eng


def _judge(store, hard_block):
    """Run one candidate through the learning gate; return (kept, action)."""
    from wolf.screener import Screener

    eng = _benched_engine(store, hard_block)
    screener = Screener.__new__(Screener)
    screener._learning = eng
    screener._learning_hard_block = hard_block
    cand = _AlwaysFires(2.0).evaluate("ZZZUSDT", [])
    kept = screener._apply_learning(cand)
    return kept, cand.learning_action, cand.score


def test_a_bench_is_recorded_not_enforced(store):
    """A bench is an absorbing state, and that is why it does not fire.

    The symbol stops emitting signals, so its record never grows, so the bench
    is permanent and can never be checked against what those trades would have
    done. It is the AI veto's blind spot under a friendlier name — and it fires
    on eight trades, where a pure coin flip returns two wins or fewer 14.5% of
    the time.
    """
    kept, action, _ = _judge(store, hard_block=False)
    assert kept is True                  # the signal still happens
    assert action == "BENCH"             # and the judgment is on the record


def test_hard_mode_still_benches(store):
    """The old behaviour is reachable, it is just no longer the default."""
    kept, action, _ = _judge(store, hard_block=True)
    assert kept is False
    assert action == "BENCH"


def test_monitor_mode_withholds_the_score_delta_too(store):
    """The delta is not cosmetic, so monitor mode cannot let it through.

    Score decides which detector's candidate wins a symbol, and bounce-risk
    shorts must clear bounce_min_score — so a swing of up to 15 points changes
    which signals exist at all. Applying it would keep the strategy drifting
    under a freeze meant to hold it still.
    """
    from wolf.screener import Screener

    eng = LearningEngine(store, LearningSettings(min_samples=3, hard_block=False))
    for _ in range(6):
        eng.observe(_resolved(symbol="AAAUSDT", status="TP_HIT", pnl=6.0))

    screener = Screener.__new__(Screener)
    screener._learning, screener._learning_hard_block = eng, False
    cand = _AlwaysFires(2.0).evaluate("AAAUSDT", [])
    before = cand.score
    assert screener._apply_learning(cand) is True
    assert cand.score == before                     # untouched
    assert cand.learning_action in ("BOOST", "PENALTY")
    assert any("[monitor]" in r for r in cand.reasons)


def test_the_shipped_default_is_monitor():
    """Shipping this as enforce would leave the freeze leaking."""
    assert LearningSettings().hard_block is False


def test_the_switch_is_reachable_from_the_environment(monkeypatch):
    from wolf.config import Settings

    assert Settings.from_env().learning.hard_block is False
    monkeypatch.setenv("LEARNING_HARD_BLOCK", "1")
    assert Settings.from_env().learning.hard_block is True
