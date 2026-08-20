"""Centralised configuration for Wolf Crypto Tracker.

All runtime configuration is loaded from environment variables into a single,
immutable :class:`Settings` object that is passed explicitly to the components
that need it. This replaces the scattered module-level ``global`` state of the
previous bot (one of the main maintainability problems) and makes the code
trivially testable: a test just constructs a ``Settings(...)`` with the values
it wants instead of mutating process-wide globals.

Environment variable names are kept identical to the previous deployment so the
existing Railway / `.env` configuration keeps working without changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

try:  # python-dotenv is optional at runtime (always present in dev).
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv missing is non-fatal
    pass


def _env_str(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return val if val is not None else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_csv(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _resolve_state_dir() -> str:
    """Where signal history lives, preferring a mounted volume.

    An explicit ``STATE_DIR`` always wins. Otherwise, when the platform reports
    an attached volume (Railway exports ``RAILWAY_VOLUME_MOUNT_PATH``), state
    goes there instead of into the container filesystem — which is discarded on
    every redeploy, silently resetting the outcome history the auto-pause and
    learning gates are supposed to be judging.

    Getting this wrong is invisible: the bot starts fine, reports zero
    outcomes, and simply never accumulates the sample it needs.
    """
    explicit = os.environ.get("STATE_DIR", "").strip()
    if explicit:
        return explicit
    mount = volume_mount()
    if mount:
        return os.path.join(mount, "state_data")
    return "state_data"


def volume_mount() -> str:
    """The platform's volume mount path, or "" when none is exported.

    Kept as its own function so diagnostics can report what was actually seen.
    Auto-detection depends on the platform exporting this, and when it does not
    the failure is silent: state quietly lands in the container and everything
    else looks healthy. Reporting the raw value turns that into something
    readable instead of a guess about which half of the setup went wrong.
    """
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()


def state_is_persistent(state_dir: str) -> bool:
    """Whether ``state_dir`` looks like it survives a redeploy.

    A relative path always resolves inside the container. An absolute path is
    only durable if it sits under the mounted volume — an absolute path
    elsewhere in the container filesystem is just as ephemeral, which is the
    case a bare "is it absolute?" check quietly passes.
    """
    if not os.path.isabs(state_dir):
        return False
    mount = volume_mount()
    if mount:
        return os.path.abspath(state_dir).startswith(os.path.abspath(mount))
    # No platform hint: an absolute path is the operator's explicit choice, so
    # take it at face value rather than crying wolf on a real bind mount.
    return True


def _env_float_csv(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """Parse ``"0.5,0.3,0.2"`` into floats; any bad entry keeps the default."""
    parts = _env_csv(name)
    if not parts:
        return default
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        return default


@dataclass(frozen=True)
class TelegramSettings:
    """Telegram bot credentials and channel/thread routing."""

    bot_token: str = ""
    chat_id: str = ""
    # Topic/thread routing (supergroup forum topics). Empty -> main channel.
    signal_thread_id: str = ""
    new_signal_thread_id: str = ""
    high_conviction_thread_id: str = ""  # 🎯 High-Conviction (TRAP) — premium tier
    market_update_thread_id: str = ""
    trade_report_thread_id: str = ""
    news_thread_id: str = ""
    system_thread_id: str = ""   # startup / health / errors
    stats_thread_id: str = ""    # periodic performance summary
    whale_thread_id: str = ""    # 👁 Whale Report (large trades)
    radar_thread_id: str = ""    # 🔥 Hot Ecosystem (market radar)
    majors_thread_id: str = ""   # 🐝 BTC/ETH/SOL (majors session report)
    flow_thread_id: str = ""     # 🧠 Flow Intelligence (defaults to News topic)
    allowed_chat_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # ── routing with graceful fallback ──
    # Each message type goes to its own topic, falling back to the main channel
    # (empty thread id) when that topic isn't configured — so nothing is dropped.
    def route_new_signal(self) -> str:      # 🆕 New Signal
        return _first(self.new_signal_thread_id)

    def route_high_conviction(self) -> str:  # 🎯 High-Conviction (TRAP)
        # No fallback chain here on purpose: an empty result tells the notifier
        # to leave the message on its normal per-event route (announce / entry /
        # trade-report), preserving existing behaviour when the topic is unset.
        return _first(self.high_conviction_thread_id)

    def route_entry(self) -> str:           # ⭐ Signal Entry (activation / TP)
        return _first(self.signal_thread_id)

    def route_market_update(self) -> str:   # 📚 Market Update (bias/pulse)
        return _first(self.market_update_thread_id)

    def route_trade_report(self) -> str:    # 📝 Trade Reports (resolutions)
        return _first(self.trade_report_thread_id)

    def route_news(self) -> str:            # 🗞 News Update
        return _first(self.news_thread_id)

    def route_flow(self) -> str:            # 🧠 Flow Intelligence → News topic by default
        return _first(self.flow_thread_id, self.news_thread_id)

    def route_whale(self) -> str:           # 👁 Whale Report
        return _first(self.whale_thread_id)

    def route_radar(self) -> str:           # 🔥 Hot Ecosystem (radar)
        return _first(self.radar_thread_id)

    def route_majors(self) -> str:          # 🐝 BTC/ETH/SOL
        return _first(self.majors_thread_id)

    def route_system(self) -> str:          # startup / health
        return _first(self.system_thread_id)

    def route_stats(self) -> str:           # periodic performance
        return _first(self.stats_thread_id, self.system_thread_id)

    def configured_threads(self) -> list[tuple[str, str]]:
        """Return ``(label, thread_id)`` for each non-empty configured topic.

        Used at startup to validate every routed topic exists, so a wrong/stale
        thread id is reported once instead of failing silently on every message.
        """
        labels = [
            ("Signal Entry", self.signal_thread_id),
            ("New Signal", self.new_signal_thread_id),
            ("High-Conviction", self.high_conviction_thread_id),
            ("Market Update", self.market_update_thread_id),
            ("Trade Report", self.trade_report_thread_id),
            ("News", self.news_thread_id),
            ("System/General", self.system_thread_id),
            ("Stats", self.stats_thread_id),
            ("Whale Report", self.whale_thread_id),
            ("Hot Ecosystem", self.radar_thread_id),
            ("BTC/ETH/SOL", self.majors_thread_id),
        ]
        return [(label, tid) for label, tid in labels if tid]


def _first(*values: str) -> str:
    for v in values:
        if v:
            return v
    return ""


@dataclass(frozen=True)
class RiskSettings:
    """Risk-management gates applied to signal emission.

    These close the loop between the bot's own results and what it trades next:
    align entries with the broad market regime, pause when the equity curve is
    bleeding, and stop emitting strategies that have proven unprofitable.
    """

    # Market-regime filter: flag trend-following LONGs in a BEARISH market and
    # SHORTs in a BULLISH one. Counter-trend reversal setups are exempt.
    regime_filter_enabled: bool = True
    regime_symbol: str = "BTCUSDT"
    regime_interval: str = "1h"

    # Drawdown throttle (always a HARD gate): pause ALL new entries once the paper
    # equity is this far below its peak (percent). Protects realized gains.
    drawdown_pause_pct: float = 15.0

    # Auto-pause underperformers, judged on realized edge in R (PnL per unit of
    # the trade's own risk) rather than in percent: targets are ATR multiples, so
    # a percent average is dominated by whichever volatile symbols traded.
    #
    # A strategy is paused only when the upper bound of its one-sided confidence
    # interval is still below the floor — avg_r + z*se_r < floor. Comparing a
    # bare average to a threshold ignores how noisy the average is; at the old
    # 12-trade minimum the standard error was ~0.4R, which is why the gate
    # flagged ~78% of signals and the flagged ones then outperformed.
    autopause_min_trades: int = 30
    # 0.0R = pause only what is confidently losing on a risk-adjusted basis.
    # Raise it to demand a margin over breakeven (fees run roughly 0.2-0.4R).
    autopause_min_expectancy_r: float = 0.0
    # 1.65 = one-sided 95%. Lower it to pause sooner on weaker evidence.
    autopause_confidence_z: float = 1.65
    autopause_min_win_rate: float = 38.0

    # Enforcement mode for the regime + auto-pause gates.
    #   False (default) = MONITOR: still emit, but flag + down-score so we collect
    #     the "what if we'd traded it" record before committing to a block.
    #   True            = HARD: drop the signal outright.
    # Drawdown is always hard regardless of these. This default is the "Campur"
    # (hybrid) setup: equity protection is enforced, judgement gates are observed.
    regime_hard_block: bool = False
    autopause_hard_block: bool = False

    # Concentration caps on concurrent open positions (PENDING + ACTIVE). Stops
    # one strategy hogging slots (e.g. a losing MOMENTUM taking 4) and caps
    # single-direction exposure so a regime flip can't hit a stack of correlated
    # shorts at once. A cap <= 0 disables that limit (unlimited).
    max_active_per_strategy: int = 4
    max_active_per_direction: int = 6

    # ── Composite regime / bounce-guard (risk-scaling on shorts) ──
    # Folds flow signals (F&G, USDT.D, dry powder, chain flow) into a macro
    # context. When a fresh SHORT faces bounce/squeeze risk (extreme fear, or
    # USDT.D rotating into risk / at a historic extreme) the guard SCALES RISK —
    # smaller size + a higher score bar — rather than blocking, because the
    # direction out of extreme fear is genuinely uncertain. Applies to ALL
    # shorts incl. counter-trend (PREDUMP/TRAP/SCALP), closing the blind spot the
    # trend-only regime filter leaves open.
    composite_regime_enabled: bool = True
    # "monitor" (default) = flag + log the what-if only, change nothing, so we
    # collect a clean W/L sample of shorts-under-bounce-risk first.
    # "live" = actually apply the size factor + selectivity floor.
    bounce_guard_mode: str = "monitor"
    fear_extreme_max: int = 25              # F&G ≤ this = extreme fear
    usdtd_riskoff_change_pct: float = 0.2   # |USDT.D 24h Δ| ≥ this = directional flag
    usdtd_reversal_percentile: float = 85.0  # USDT.D above this percentile = reversal risk
    usdtd_history_days: int = 90            # rolling window kept for percentile
    usdtd_min_history_days: int = 7         # min history before percentile is trusted
    dry_powder_outflow_pct: float = -0.5    # stablecoin 1d Δ ≤ this = risk-off
    flow_context_ttl_min: int = 30          # cache TTL for slow-moving flow dims
    bounce_size_factor: float = 0.5         # LIVE: shrink short risk to this fraction
    bounce_min_score: int = 88              # LIVE: bounce-risk shorts need ≥ this score

    # ── Trade-plan / position-sizing engine (surfaced to the user per signal) ──
    # Turns each signal into an executable plan: suggested leverage, margin and
    # the liquidation price, sized so a stop-out costs exactly ``paper_risk_pct``
    # of balance and liquidation can never trigger before the stop.
    plan_enabled: bool = True
    # Largest leverage the bot will ever recommend (beginner-safe per the guide).
    max_leverage: int = 10
    # Exchange maintenance-margin rate (≈0.5% for majors USDⓈ-M) for liq math.
    maintenance_margin_rate: float = 0.005
    # Liquidation must sit at least this many times the stop distance away, so
    # the stop is always hit first with comfortable room to spare.
    liq_safety_buffer: float = 2.0


@dataclass(frozen=True)
class LadderSettings:
    """Target geometry — one place decides every signal's reward:risk.

    Distinct from :class:`RiskSettings`, which gates *whether* a signal is
    emitted. This decides *where* its targets go once it is.

    Detectors choose only how far the stop sits from entry — often at a
    structural level rather than a fixed ATR multiple — and that distance is
    1R. Every rung is then placed at a fraction of ``rr_target`` R, so the
    advertised ratio holds across symbols and volatility regimes and no
    detector can quietly ship a 1:1 while the others run 1:3.

    ``tp_allocations`` is the fraction of the position closed at each rung, and
    it is not decoration: reward:risk describes only the *final* rung, so a 1:3
    signal that takes half off at 1R does not return 3R. The tracker grades on
    what was realised, which makes the split part of the geometry.
    """

    rr_target: float = 3.0
    # Rung placement as fractions of the target — (1/3, 2/3, 1) of 3R.
    tp_ladder_fractions: tuple[float, ...] = (1 / 3, 2 / 3, 1.0)
    # Position fraction closed at each rung. Front-loaded: the near rung is the
    # one price actually reaches, so it carries the most size.
    tp_allocations: tuple[float, ...] = (0.5, 0.3, 0.2)
    # When one candle reaches both a take-profit and the stop, infer the order
    # from the bar's own direction instead of always assuming the stop went
    # first. See Tracker._evaluate for why inferring beats either fixed rule.
    intrabar_tp_first: bool = True

    def allocations_for(self, rungs: int) -> tuple[float, ...]:
        """Allocations resized to a ladder of ``rungs`` rungs.

        Detectors emit ladders of different lengths and the tracker drops rungs
        on the wrong side of entry, so the configured weights are truncated and
        renormalised — the position is always fully closed, never partly
        unaccounted for.
        """
        if rungs <= 0:
            return ()
        weights = list(self.tp_allocations[:rungs])
        while len(weights) < rungs:
            weights.append(0.0)
        total = sum(weights)
        if total <= 0:
            return tuple(1 / rungs for _ in range(rungs))
        return tuple(w / total for w in weights)

    @property
    def full_run_r(self) -> float:
        """R banked when every rung fills — the ladder's real ceiling.

        Not ``rr_target``: only the last slice earns the headline number. With
        the defaults a perfect trade returns ~1.7R, and that is what a win rate
        has to be judged against.
        """
        fractions = [f for f in self.tp_ladder_fractions if f > 0] or [1.0]
        allocations = self.allocations_for(len(fractions))
        return sum(a * self.rr_target * f for a, f in zip(allocations, fractions))

    @property
    def breakeven_win_rate(self) -> float:
        """Win rate needed to break even at this geometry, before costs."""
        run = self.full_run_r
        return 100 / (1 + run) if run > 0 else 100.0


@dataclass(frozen=True)
class UniverseSettings:
    """How the screener chooses which symbols to scan.

    Static mode scans a fixed majors list. Dynamic mode ranks the whole market
    by 24h quote volume (one API call) and scans the most liquid pairs — so meme
    coins and other ecosystems rotate in as they get active, instead of only the
    same hardcoded majors. The core majors are always included as a stable base.
    """

    dynamic: bool = True
    top_n: int = 30                       # how many volume leaders to scan
    min_quote_volume: float = 10_000_000  # liquidity floor (USDT 24h quote vol)
    quote: str = "USDT"


@dataclass(frozen=True)
class TrackerSettings:
    """Signal-tracking behaviour knobs."""

    # Per signal-type timeout (hours) before a pending signal expires.
    # A timeout has to be read against the timeframe the setup was found on:
    # the targets are ATR multiples of that series, so the time it plausibly
    # needs scales with it. Roughly 40 bars of the detector's own interval —
    # long enough to reach the 3R rung, short enough that a dead trade frees
    # its slot. Capping a 4h swing at 24h (six bars) is what turned every
    # signal into a scalp no matter which detector produced it.
    timeout_screener_h: int = 48   # MOMENTUM — 1h candles
    timeout_prepump_h: int = 48    # 1h candles
    timeout_predump_h: int = 48    # 1h candles
    timeout_scalp_h: int = 10      # 15m candles
    timeout_swing_h: int = 168     # 4h candles — a real swing runs for days
    timeout_trap_h: int = 4        # 15m — liquidity-trap reversals resolve fast
    timeout_news_h: int = 4        # news-driven signals expire quickly
    # Per-strategy dedup windows (minutes).  Tighter for fast setups (SCALP
    # expires in 2 h so there is no point blocking a fresh sweep for 30 min),
    # wider for slow setups (SWING holds 24 h, so 60 min avoids noise re-entries).
    # ``dedup_minutes`` is kept as the legacy fallback for unknown strategy types.
    dedup_minutes: int = 30       # legacy / fallback
    dedup_scalp_min: int = 10
    # Dedup windows follow the same logic: roughly one bar of the detector's
    # own timeframe, so the same setup is not re-emitted several times inside
    # a single candle it has not finished forming.
    dedup_prepump_min: int = 60      # 1h candles
    dedup_predump_min: int = 60      # 1h candles
    dedup_screener_min: int = 60     # 1h candles
    dedup_swing_min: int = 240       # 4h candles

    # Keep at most this many resolved outcomes on disk. At ~65 outcomes/day the
    # old 500 cap silently discarded the oldest records after roughly a week,
    # which quietly turned "all-time" stats into a rolling window.
    max_outcomes: int = 5000

    # A signal that times out within this many R of entry is graded EXPIRED_FLAT
    # (no verdict) instead of being scored as a win or a loss on noise.
    expiry_flat_r: float = 0.25

    # Grading: once TP1 (the first ladder rung) is banked, a later stop-out at
    # breakeven is booked as a partial win (models a scaled exit — part off at
    # TP1, the rest rides to BE) instead of a loss. This stops trend setups that
    # reliably reach TP1 from being scored as serial losers by the all-or-nothing
    # rule. Off keeps the legacy rule: a win only when the final rung is
    # reached; a post-TP1 breakeven stop counts as a loss. On by default — at a
    # 1:3 ladder, "reached TP1 then came back" is the most common shape of a
    # profitable signal, so the legacy rule mis-grades most of the winners.
    tp1_banks_win: bool = True

    def timeout_for(self, signal_type: str) -> int:
        return {
            "SCREENER": self.timeout_screener_h,
            "PREPUMP": self.timeout_prepump_h,
            "PREDUMP": self.timeout_predump_h,
            "SCALP": self.timeout_scalp_h,
            "SWING": self.timeout_swing_h,
            "TRAP": self.timeout_trap_h,
            "NEWS": self.timeout_news_h,
        }.get(signal_type.upper(), self.timeout_screener_h)

    def dedup_for(self, signal_type: str) -> int:
        """Return the dedup window in minutes for a given signal type."""
        return {
            "SCALP": self.dedup_scalp_min,
            "PREPUMP": self.dedup_prepump_min,
            "PREDUMP": self.dedup_predump_min,
            "SCREENER": self.dedup_screener_min,
            "SWING": self.dedup_swing_min,
        }.get(signal_type.upper(), self.dedup_minutes)


@dataclass(frozen=True)
class NewsSettings:
    """Crypto-news posting configuration."""

    enabled: bool = False
    provider: str = "cryptocompare"  # free, key-less (single-source / legacy)
    # Multi-source fan-out (CSV): any of cryptocompare, reddit, hackernews.
    sources: tuple[str, ...] = ("cryptocompare",)
    interval_min: int = 30
    max_items: int = 3
    # Synthesise fresh headlines into one AI brief instead of a flat card.
    synthesis_enabled: bool = False
    narrator_provider: str = "deepseek"
    narrator_model: str = ""
    # Generate trading signals from news headlines.
    signals_enabled: bool = False


@dataclass(frozen=True)
class ReportsSettings:
    """Periodic market reports posted to their own Telegram topics."""

    # 🐝 BTC/ETH/SOL session report
    majors_enabled: bool = False
    majors_interval_min: int = 60
    # 🔥 Hot Ecosystem — market radar (gainers/losers/volume)
    radar_enabled: bool = False
    radar_interval_min: int = 30
    radar_min_quote_volume: float = 5_000_000
    # 📚 Market Update — BTC/ETH bias pulse
    pulse_enabled: bool = False
    pulse_interval_min: int = 30
    # 👁 Whale Report — large trades
    whale_enabled: bool = False
    whale_interval_min: int = 5
    whale_min_usd: float = 250_000


@dataclass(frozen=True)
class DebateRole:
    """One debate participant (bull / bear / arbiter) → a provider + model."""

    provider: str  # deepseek | groq | hermes | anthropic
    model: str


@dataclass(frozen=True)
class FlowSettings:
    """Flow Intelligence digest — reads collector snapshots, posts a digest.

    ``interval_min`` is the *reporting* cadence only. How fresh the numbers are
    is set by the collectors below (:class:`OnChainSettings`), which is the
    point of the split: the digest can be re-rendered as often as you like
    without costing a single extra API call.
    """

    enabled: bool = False
    interval_min: int = 30            # digest cadence; rendering is free
    markets_limit: int = 60           # CoinGecko coins the macro collector screens
    max_watch: int = 5                # watchlist length
    # LLM narrator for the on-demand single-token deep dive (POST /flow/{symbol}).
    # The digest itself is deterministic — its numbers are never phrased by a model.
    narrator_provider: str = "deepseek"
    narrator_model: str = ""


@dataclass(frozen=True)
class OnChainSettings:
    """On-chain / whale / institutional collectors.

    Each is independently switchable, and each is optional: with all of them off
    the bot behaves exactly as it did before they existed. Intervals are chosen
    against what the free endpoints tolerate, not against how often the data
    could theoretically change.
    """

    # Valuation (CoinGecko + DefiLlama). Hourly: fundamentals move slowly and
    # the public CoinGecko API will not carry a 15-symbol universe any faster.
    valuation_enabled: bool = False
    valuation_interval_min: int = 60
    valuation_cache_ttl_sec: int = 3600
    valuation_rate_limit_backoff_sec: int = 900
    valuation_max_symbols: int = 15    # cap per run, so a wide universe cannot
                                       # spend the whole rate-limit window

    # Hyperliquid whale scan. 10 minutes matches the screening cycle, so a
    # signal is judged against positioning no older than one cycle.
    whale_enabled: bool = False
    whale_interval_min: int = 10
    whale_top_wallets: int = 30
    whale_min_position_usd: float = 30_000.0
    whale_min_wallets: int = 3         # coordination threshold for an alert
    whale_cooldown_min: int = 60       # per-coin quiet period after alerting
    # Post detected coordination to the 👁 Whale Report topic as an event.
    # Separate from the Flow Intelligence digest on purpose: this fires when
    # wallets pile in, that one reports positioning on a timer.
    whale_alert_enabled: bool = True

    # Coinbase premium (BTC only).
    premium_enabled: bool = False
    premium_interval_min: int = 10

    # Market-wide macro/dry-powder/rotation snapshot for the digest.
    macro_enabled: bool = False
    macro_interval_min: int = 60

    # How old a snapshot may be before the signal path treats it as absent.
    # Below this, gates and the AI debate see it; above, they degrade to
    # candle-only, which the bot already handles.
    staleness_min: int = 30

    # Whale veto gate in the screener. Deliberately stricter than the alert
    # threshold: 3 wallets is worth reporting, overriding a setup takes 5.
    whale_veto_enabled: bool = True
    whale_veto_min_wallets: int = 5

    @property
    def any_enabled(self) -> bool:
        return any((self.valuation_enabled, self.whale_enabled,
                    self.premium_enabled, self.macro_enabled))


@dataclass(frozen=True)
class AnomalySettings:
    """Anomaly scanner (PAPER MODE) — appended as a section to the flow report.

    Scans a volume-ordered slice of the mid-cap universe for coiling / volume
    anomalies, scores them, and logs every signal ≥ ``min_score`` to a Google
    Sheet for later expectancy evaluation. Never executes — advisory only.
    """

    enabled: bool = False
    min_score: int = 55
    max_picks: int = 3
    paper_mode: bool = True            # guard flag: always paper, no execution
    # Runtime discipline (CoinGecko free tier · Railway < 8 min/cycle).
    scan_limit: int = 50               # coins scanned per cycle (top by volume)
    time_budget_sec: int = 360         # wall-clock stop for the scan loop
    # Google-Sheet paper log (Phase 6).
    paper_log_enabled: bool = False
    sheet_name: str = "Anomaly_Paper_Log"
    sheets_credentials: str = ""       # service-account JSON (raw or file path)
    backfill_interval_hours: int = 24


@dataclass(frozen=True)
class AISettings:
    """AI debate-layer configuration.

    All three roles default to DeepSeek so a single DEEPSEEK_API_KEY is enough
    to run the full debate — and ``from_env`` now agrees. It previously defaulted
    bear to Groq and arbiter to Hermes, quietly requiring three keys: with only
    DEEPSEEK_API_KEY set the arbiter fell back to a null client, returned no
    JSON, and every signal abstained without logging anything.

    Override individual roles via env vars for a multi-provider debate
    (DEBATE_BEAR_PROVIDER=groq, DEBATE_ARBITER_PROVIDER=hermes, ...). Each
    provider needs its own key; a role whose key is missing is reported at
    startup rather than silently degrading the debate.

    Enable with: AI_DEBATE_ENABLED=true  (not AI_ENABLED)
    """

    enabled: bool = False
    bull: DebateRole = DebateRole("deepseek", "deepseek-chat")
    bear: DebateRole = DebateRole("deepseek", "deepseek-chat")
    arbiter: DebateRole = DebateRole("deepseek", "deepseek-chat")
    # If a REJECT verdict at/above this confidence should veto the signal.
    veto_enabled: bool = True
    veto_min_confidence: int = 70
    # Pass the last N candles to the AI as raw price data (0 = text-only mode).
    chart_candles: int = 20


@dataclass(frozen=True)
class LearningSettings:
    """Adaptive learning knobs — how strongly memory tunes live screening."""

    enabled: bool = True
    min_samples: int = 5              # trades before a win-rate adjusts the score
    max_adjust: float = 15.0         # max +/- score points learning may apply
    blacklist_min_trades: int = 8    # bench a symbol after this many trades...
    blacklist_max_winrate: float = 25.0  # ...if its win-rate is below this %


@dataclass(frozen=True)
class BacktestSettings:
    """Backtest / warm-start settings."""

    lookback: int = 50               # candles replayed per symbol at warm-start
    candle_limit: int = 250
    warm_start: bool = True          # seed learning from a backtest at boot


@dataclass(frozen=True)
class Settings:
    """Top-level immutable application settings."""

    # Storage
    state_dir: str = "state_data"

    # Exchange
    binance_spot_base: str = "https://api.binance.com/api/v3"
    binance_futures_base: str = "https://fapi.binance.com"
    http_timeout: float = 10.0
    # Ordered data sources to try (fallback). First that responds wins.
    exchanges: tuple[str, ...] = ("binance", "okx", "bybit", "gate")

    # Paper trading account (drives the Trade Report balance/PnL view)
    paper_start_balance: float = 1000.0
    paper_risk_pct: float = 1.0

    # Scheduling (minutes)
    screener_interval_min: int = 10
    tracker_interval_min: int = 5
    # Periodic performance summary to Telegram (hours; 0 disables).
    stats_report_hours: int = 24
    # All-in round-trip trading cost in basis points (taker both sides plus
    # slippage), used to report expectancy net of costs. It matters more than it
    # looks: targets are ATR multiples, so 1R is often well under 1% and 20bps
    # can be a third of the risk unit.
    round_trip_cost_bps: float = 20.0

    # Refuse a signal whose stop sits so close that the round trip eats more
    # than this fraction of 1R. A stop 0.12% from entry pays 1.67R in fees at
    # 20bps — the trade is a guaranteed loser before it starts, however good
    # the setup or the reward:risk ratio looks. Measured, not theoretical: a
    # 24h sample had the two tightest-stop strategies at cost 1.05R and 1.67R
    # while the widest ran 0.25R, and only the wide ones survived costs.
    max_cost_r: float = 0.5

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # When set, state-mutating endpoints require this value in the X-API-Key
    # header. Empty (default) leaves the API open — convenient for local dev.
    api_key: str = ""

    # Optional integrations (kept identical to the previous bot)
    gemini_api_key: str = ""
    groq_api_key: str = ""
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    hermes_api_key: str = ""
    newsapi_key: str = ""
    twitter_bearer_token: str = ""
    glassnode_api_key: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""

    log_level: str = "INFO"

    # Display timezone for message timestamps (IANA name). Default WIB.
    timezone: str = "Asia/Jakarta"

    # Reject a setup when measured order flow opposes it (see wolf.orderflow).
    flow_veto: bool = True

    # Minimum reward:risk ratio to emit a signal. Sits just below
    # ``LadderSettings.rr_target`` so rounding and structural stops don't trip
    # it, while anything materially under the policy is still dropped.
    min_signal_rr: float = 2.5

    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    tracker: TrackerSettings = field(default_factory=TrackerSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    ladder: LadderSettings = field(default_factory=LadderSettings)
    universe: UniverseSettings = field(default_factory=UniverseSettings)
    ai: AISettings = field(default_factory=AISettings)
    news: NewsSettings = field(default_factory=NewsSettings)
    reports: ReportsSettings = field(default_factory=ReportsSettings)
    flow: FlowSettings = field(default_factory=FlowSettings)
    onchain: OnChainSettings = field(default_factory=OnChainSettings)
    anomaly: AnomalySettings = field(default_factory=AnomalySettings)
    learning: LearningSettings = field(default_factory=LearningSettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)

    @classmethod
    def from_env(cls) -> "Settings":
        """Build a :class:`Settings` from the current environment."""
        telegram = TelegramSettings(
            bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
            chat_id=_env_str("TELEGRAM_CHAT_ID"),
            signal_thread_id=_env_str("SIGNAL_THREAD_ID"),
            new_signal_thread_id=_env_str("NEW_SIGNAL_THREAD_ID"),
            high_conviction_thread_id=_env_str("HIGH_CONVICTION_THREAD_ID"),
            market_update_thread_id=_env_str("MARKET_UPDATE_THREAD_ID"),
            trade_report_thread_id=_env_str("TRADE_REPORT_THREAD_ID"),
            news_thread_id=_env_str("NEWS_THREAD_ID"),
            system_thread_id=_env_str("SYSTEM_THREAD_ID"),
            stats_thread_id=_env_str("STATS_THREAD_ID"),
            whale_thread_id=_env_str("WHALE_THREAD_ID"),
            radar_thread_id=_env_str("RADAR_THREAD_ID"),
            majors_thread_id=_env_str("MAJORS_THREAD_ID"),
            flow_thread_id=_env_str("FLOW_THREAD_ID"),
            allowed_chat_ids=_env_csv("ALLOWED_CHAT_IDS"),
        )
        news_provider = _env_str("NEWS_PROVIDER", "cryptocompare")
        news_sources = _env_csv("NEWS_SOURCES") or (news_provider,)
        news = NewsSettings(
            enabled=_env_bool("NEWS_ENABLED", False),
            provider=news_provider,
            sources=tuple(news_sources),
            interval_min=_env_int("NEWS_INTERVAL_MIN", 30),
            max_items=_env_int("NEWS_MAX_ITEMS", 3),
            synthesis_enabled=_env_bool("NEWS_SYNTHESIS_ENABLED", False),
            narrator_provider=_env_str("NEWS_NARRATOR_PROVIDER", "deepseek"),
            narrator_model=_env_str("NEWS_NARRATOR_MODEL", ""),
            signals_enabled=_env_bool("NEWS_SIGNALS_ENABLED", False),
        )
        reports = ReportsSettings(
            majors_enabled=_env_bool("MAJORS_ENABLED", False),
            majors_interval_min=_env_int("MAJORS_INTERVAL_MIN", 60),
            radar_enabled=_env_bool("RADAR_ENABLED", False),
            radar_interval_min=_env_int("RADAR_INTERVAL_MIN", 30),
            radar_min_quote_volume=_env_float("RADAR_MIN_QUOTE_VOLUME", 5_000_000),
            pulse_enabled=_env_bool("MARKET_PULSE_ENABLED", False),
            pulse_interval_min=_env_int("MARKET_PULSE_INTERVAL", 30),
            whale_enabled=_env_bool("WHALE_ENABLED", False),
            whale_interval_min=_env_int("WHALE_INTERVAL_MIN", 5),
            whale_min_usd=_env_float("WHALE_MIN_USD", 250_000),
        )
        flow = FlowSettings(
            enabled=_env_bool("FLOW_ENABLED", False),
            interval_min=_env_int("FLOW_INTERVAL_MIN", 30),
            markets_limit=_env_int("FLOW_MARKETS_LIMIT", 60),
            max_watch=_env_int("FLOW_MAX_WATCH", 5),
            narrator_provider=_env_str("FLOW_NARRATOR_PROVIDER", "deepseek"),
            narrator_model=_env_str("FLOW_NARRATOR_MODEL", ""),
        )
        onchain = OnChainSettings(
            valuation_enabled=_env_bool("ONCHAIN_VALUATION_ENABLED", False),
            valuation_interval_min=_env_int("ONCHAIN_VALUATION_INTERVAL_MIN", 60),
            valuation_cache_ttl_sec=_env_int("ONCHAIN_VALUATION_CACHE_TTL_SEC", 3600),
            valuation_rate_limit_backoff_sec=_env_int("ONCHAIN_VALUATION_BACKOFF_SEC", 900),
            valuation_max_symbols=_env_int("ONCHAIN_VALUATION_MAX_SYMBOLS", 15),
            whale_enabled=_env_bool("WHALE_HL_ENABLED", False),
            whale_interval_min=_env_int("WHALE_HL_INTERVAL_MIN", 10),
            whale_top_wallets=_env_int("WHALE_HL_TOP_WALLETS", 30),
            whale_min_position_usd=_env_float("WHALE_HL_MIN_POSITION_USD", 30_000.0),
            whale_min_wallets=_env_int("WHALE_HL_MIN_WALLETS", 3),
            whale_cooldown_min=_env_int("WHALE_HL_COOLDOWN_MIN", 60),
            whale_alert_enabled=_env_bool("WHALE_HL_ALERT_ENABLED", True),
            premium_enabled=_env_bool("COINBASE_PREMIUM_ENABLED", False),
            premium_interval_min=_env_int("COINBASE_PREMIUM_INTERVAL_MIN", 10),
            macro_enabled=_env_bool("FLOW_MACRO_ENABLED", False),
            macro_interval_min=_env_int("FLOW_MACRO_INTERVAL_MIN", 60),
            staleness_min=_env_int("ONCHAIN_STALENESS_MIN", 30),
            whale_veto_enabled=_env_bool("WHALE_VETO_ENABLED", True),
            whale_veto_min_wallets=_env_int("WHALE_VETO_MIN_WALLETS", 5),
        )
        anomaly = AnomalySettings(
            enabled=_env_bool("ANOMALY_ENABLED", False),
            min_score=_env_int("ANOMALY_MIN_SCORE", 55),
            max_picks=_env_int("ANOMALY_MAX_PICKS", 3),
            paper_mode=_env_bool("ANOMALY_PAPER_MODE", True),
            scan_limit=_env_int("ANOMALY_SCAN_LIMIT", 50),
            time_budget_sec=_env_int("ANOMALY_TIME_BUDGET_SEC", 360),
            paper_log_enabled=_env_bool("ANOMALY_PAPER_ENABLED", False),
            sheet_name=_env_str("ANOMALY_SHEET_NAME", "Anomaly_Paper_Log"),
            sheets_credentials=_env_str("GOOGLE_SHEETS_CREDENTIALS", ""),
            backfill_interval_hours=_env_int("ANOMALY_BACKFILL_INTERVAL_HOURS", 24),
        )
        learning = LearningSettings(
            enabled=_env_bool("LEARNING_ENABLED", True),
            min_samples=_env_int("LEARNING_MIN_SAMPLES", 5),
            max_adjust=_env_float("LEARNING_MAX_ADJUST", 15.0),
            blacklist_min_trades=_env_int("LEARNING_BLACKLIST_MIN_TRADES", 8),
            blacklist_max_winrate=_env_float("LEARNING_BLACKLIST_MAX_WINRATE", 25.0),
        )
        backtest = BacktestSettings(
            lookback=_env_int("BACKTEST_LOOKBACK", 50),
            candle_limit=_env_int("BACKTEST_CANDLE_LIMIT", 250),
            warm_start=_env_bool("BACKTEST_WARM_START", True),
        )
        # Defaults mirror the dataclass, which ties each window to the
        # timeframe its detector reads — see TrackerSettings.
        _t = TrackerSettings()
        dedup_default = _env_int("TRACKER_DEDUP_MINUTES", _t.dedup_minutes)
        tracker = TrackerSettings(
            dedup_minutes=dedup_default,
            dedup_scalp_min=_env_int("TRACKER_DEDUP_SCALP_MIN", _t.dedup_scalp_min),
            dedup_prepump_min=_env_int("TRACKER_DEDUP_PREPUMP_MIN", _t.dedup_prepump_min),
            dedup_predump_min=_env_int("TRACKER_DEDUP_PREDUMP_MIN", _t.dedup_predump_min),
            dedup_screener_min=_env_int("TRACKER_DEDUP_SCREENER_MIN", _t.dedup_screener_min),
            dedup_swing_min=_env_int("TRACKER_DEDUP_SWING_MIN", _t.dedup_swing_min),
            timeout_screener_h=_env_int("TRACKER_TIMEOUT_SCREENER_H", _t.timeout_screener_h),
            timeout_prepump_h=_env_int("TRACKER_TIMEOUT_PREPUMP_H", _t.timeout_prepump_h),
            timeout_predump_h=_env_int("TRACKER_TIMEOUT_PREDUMP_H", _t.timeout_predump_h),
            timeout_scalp_h=_env_int("TRACKER_TIMEOUT_SCALP_H", _t.timeout_scalp_h),
            timeout_swing_h=_env_int("TRACKER_TIMEOUT_SWING_H", _t.timeout_swing_h),
            max_outcomes=_env_int("TRACKER_MAX_OUTCOMES", _t.max_outcomes),
            tp1_banks_win=_env_bool("TRACKER_TP1_BANKS_WIN", _t.tp1_banks_win),
            expiry_flat_r=_env_float("TRACKER_EXPIRY_FLAT_R", _t.expiry_flat_r),
        )
        ladder = LadderSettings(
            rr_target=_env_float("RISK_RR_TARGET", 3.0),
            tp_ladder_fractions=_env_float_csv("RISK_TP_FRACTIONS", (1 / 3, 2 / 3, 1.0)),
            tp_allocations=_env_float_csv("RISK_TP_ALLOCATIONS", (0.5, 0.3, 0.2)),
            intrabar_tp_first=_env_bool("INTRABAR_TP_FIRST", True),
        )
        risk = RiskSettings(
            regime_filter_enabled=_env_bool("REGIME_FILTER_ENABLED", True),
            regime_symbol=_env_str("REGIME_SYMBOL", "BTCUSDT"),
            regime_interval=_env_str("REGIME_INTERVAL", "1h"),
            drawdown_pause_pct=_env_float("DRAWDOWN_PAUSE_PCT", 15.0),
            autopause_min_trades=_env_int("AUTOPAUSE_MIN_TRADES", 30),
            autopause_min_expectancy_r=_env_float("AUTOPAUSE_MIN_EXPECTANCY_R", 0.0),
            autopause_confidence_z=_env_float("AUTOPAUSE_CONFIDENCE_Z", 1.65),
            autopause_min_win_rate=_env_float("AUTOPAUSE_MIN_WIN_RATE", 38.0),
            regime_hard_block=_env_bool("REGIME_HARD_BLOCK", False),
            autopause_hard_block=_env_bool("AUTOPAUSE_HARD_BLOCK", False),
            max_active_per_strategy=_env_int("MAX_ACTIVE_PER_STRATEGY", 4),
            max_active_per_direction=_env_int("MAX_ACTIVE_PER_DIRECTION", 6),
            composite_regime_enabled=_env_bool("COMPOSITE_REGIME_ENABLED", True),
            bounce_guard_mode=_env_str("BOUNCE_GUARD_MODE", "monitor"),
            fear_extreme_max=_env_int("FEAR_EXTREME_MAX", 25),
            usdtd_riskoff_change_pct=_env_float("USDTD_RISKOFF_CHANGE_PCT", 0.2),
            usdtd_reversal_percentile=_env_float("USDTD_REVERSAL_PERCENTILE", 85.0),
            usdtd_history_days=_env_int("USDTD_HISTORY_DAYS", 90),
            usdtd_min_history_days=_env_int("USDTD_MIN_HISTORY_DAYS", 7),
            dry_powder_outflow_pct=_env_float("DRY_POWDER_OUTFLOW_PCT", -0.5),
            flow_context_ttl_min=_env_int("FLOW_CONTEXT_TTL_MIN", 30),
            bounce_size_factor=_env_float("BOUNCE_SIZE_FACTOR", 0.5),
            bounce_min_score=_env_int("BOUNCE_MIN_SCORE", 88),
            plan_enabled=_env_bool("TRADE_PLAN_ENABLED", True),
            max_leverage=_env_int("MAX_LEVERAGE", 10),
            maintenance_margin_rate=_env_float("MAINTENANCE_MARGIN_RATE", 0.005),
            liq_safety_buffer=_env_float("LIQ_SAFETY_BUFFER", 2.0),
        )
        universe = UniverseSettings(
            dynamic=_env_bool("UNIVERSE_DYNAMIC", True),
            top_n=_env_int("UNIVERSE_TOP_N", 30),
            min_quote_volume=_env_float("UNIVERSE_MIN_QUOTE_VOLUME", 10_000_000),
        )
        ai = AISettings(
            enabled=_env_bool("AI_DEBATE_ENABLED", False),
            bull=DebateRole(
                provider=_env_str("DEBATE_BULL_PROVIDER", "deepseek"),
                model=_env_str("DEBATE_BULL_MODEL", "deepseek-chat"),
            ),
            bear=DebateRole(
                provider=_env_str("DEBATE_BEAR_PROVIDER", "deepseek"),
                model=_env_str("DEBATE_BEAR_MODEL", "deepseek-chat"),
            ),
            arbiter=DebateRole(
                provider=_env_str("DEBATE_ARBITER_PROVIDER", "deepseek"),
                model=_env_str("DEBATE_ARBITER_MODEL", "deepseek-chat"),
            ),
            veto_enabled=_env_bool("AI_VETO_ENABLED", True),
            veto_min_confidence=_env_int("AI_VETO_MIN_CONFIDENCE", 70),
            chart_candles=_env_int("AI_CHART_CANDLES", 20),
        )
        exchanges = _env_csv("EXCHANGES") or ("binance", "okx", "bybit", "gate")
        return cls(
            state_dir=_resolve_state_dir(),
            paper_start_balance=_env_float("PAPER_START_BALANCE", 1000.0),
            paper_risk_pct=_env_float("PAPER_RISK_PCT", 1.0),
            http_timeout=_env_float("HTTP_TIMEOUT", 10.0),
            exchanges=tuple(exchanges),
            screener_interval_min=_env_int("SCREENER_INTERVAL_MIN", 10),
            tracker_interval_min=_env_int("TRACKER_INTERVAL_MIN", 5),
            stats_report_hours=_env_int("STATS_REPORT_HOURS", 24),
            round_trip_cost_bps=_env_float("ROUND_TRIP_COST_BPS", 20.0),
            max_cost_r=_env_float("MAX_COST_R", 0.5),
            api_host=_env_str("API_HOST", "0.0.0.0"),
            api_port=_env_int("API_PORT", 8000),
            api_key=_env_str("API_KEY"),
            gemini_api_key=_env_str("GEMINI_API_KEY"),
            groq_api_key=_env_str("GROQ_API_KEY"),
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY"),
            deepseek_api_key=_env_str("DEEPSEEK_API_KEY"),
            hermes_api_key=_env_str("HERMES_API_KEY"),
            newsapi_key=_env_str("NEWSAPI_KEY"),
            twitter_bearer_token=_env_str("TWITTER_BEARER_TOKEN"),
            glassnode_api_key=_env_str("GLASSNODE_API_KEY"),
            supabase_url=_env_str("SUPABASE_URL"),
            supabase_anon_key=_env_str("SUPABASE_ANON_KEY"),
            log_level=_env_str("LOG_LEVEL", "INFO"),
            timezone=_env_str("TIMEZONE", "Asia/Jakarta"),
            min_signal_rr=_env_float("MIN_SIGNAL_RR", 2.5),
            flow_veto=_env_bool("FLOW_VETO", True),
            telegram=telegram,
            tracker=tracker,
            risk=risk,
            ladder=ladder,
            universe=universe,
            ai=ai,
            news=news,
            reports=reports,
            flow=flow,
            onchain=onchain,
            anomaly=anomaly,
            learning=learning,
            backtest=backtest,
        )

    def api_key_for(self, provider: str) -> str:
        """Return the configured API key for a debate provider name."""
        return {
            "anthropic": self.anthropic_api_key,
            "deepseek": self.deepseek_api_key,
            "groq": self.groq_api_key,
            "hermes": self.hermes_api_key,
            "openrouter": self.hermes_api_key,
            "gemini": self.gemini_api_key,
        }.get((provider or "").lower(), "")

    def describe(self) -> dict:
        """Return a redacted, JSON-serialisable view for diagnostics/health."""
        secret_names = {f.name for f in fields(self) if f.name.endswith(("_key", "_token"))}
        out: dict = {}
        for f in fields(self):
            if f.name in ("telegram", "tracker", "ai", "news", "reports", "flow",
                          "learning", "backtest"):
                continue
            value = getattr(self, f.name)
            if f.name in secret_names or f.name.endswith("_anon_key"):
                out[f.name] = bool(value)  # only reveal presence
            else:
                out[f.name] = value
        out["telegram_enabled"] = self.telegram.enabled
        out["ai_enabled"] = self.ai.enabled
        out["news_enabled"] = self.news.enabled
        out["flow_enabled"] = self.flow.enabled
        out["learning_enabled"] = self.learning.enabled
        return out
