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

    # Auto-pause underperformers: once a strategy has at least this many graded
    # trades, judge it on realized edge (expectancy = avg PnL % per trade) and
    # flag it when that edge falls below the floor. Win-rate is only a fallback
    # for older stats that don't carry avg_pnl — a low-WR / high-R:R setup can
    # still be net profitable, so win-rate alone is the wrong gate.
    autopause_min_trades: int = 12
    # +0.10% buffer above breakeven: a near-flat strategy (e.g. +0.05% avg) is
    # noise that turns net-negative after real fees/slippage, so require a
    # margin over zero rather than merely "not losing" on paper.
    autopause_min_expectancy: float = 0.10
    autopause_min_win_rate: float = 38.0

    # Enforcement mode for the regime + auto-pause gates.
    #   False (default) = MONITOR: still emit, but flag + down-score so we collect
    #     the "what if we'd traded it" record before committing to a block.
    #   True            = HARD: drop the signal outright.
    # Drawdown is always hard regardless of these. This default is the "Campur"
    # (hybrid) setup: equity protection is enforced, judgement gates are observed.
    regime_hard_block: bool = False
    autopause_hard_block: bool = False

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
    timeout_screener_h: int = 24
    timeout_prepump_h: int = 12
    timeout_predump_h: int = 12
    timeout_scalp_h: int = 2
    timeout_swing_h: int = 24
    timeout_trap_h: int = 4  # liquidity-trap reversals resolve fast
    timeout_news_h: int = 4  # news-driven signals expire quickly
    # Per-strategy dedup windows (minutes).  Tighter for fast setups (SCALP
    # expires in 2 h so there is no point blocking a fresh sweep for 30 min),
    # wider for slow setups (SWING holds 24 h, so 60 min avoids noise re-entries).
    # ``dedup_minutes`` is kept as the legacy fallback for unknown strategy types.
    dedup_minutes: int = 30       # legacy / fallback
    dedup_scalp_min: int = 10
    dedup_prepump_min: int = 20
    dedup_predump_min: int = 20
    dedup_screener_min: int = 30
    dedup_swing_min: int = 60

    # Keep at most this many resolved outcomes on disk.
    max_outcomes: int = 500

    # Grading: once TP1 (the first ladder rung) is banked, a later stop-out at
    # breakeven is booked as a partial win (models a scaled exit — part off at
    # TP1, the rest rides to BE) instead of a loss. This stops trend setups that
    # reliably reach TP1 from being scored as serial losers by the all-or-nothing
    # rule. Off (default) keeps the legacy rule: a win only when the final rung
    # is reached; a post-TP1 breakeven stop counts as a loss.
    tp1_banks_win: bool = False

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
    """On-chain flow-intelligence report (Nansen-style thread → News topic)."""

    enabled: bool = False
    interval_min: int = 240            # 4h — flows move slowly; avoid spam
    markets_limit: int = 60           # CoinGecko coins to scan (by volume)
    max_picks: int = 3
    max_skips: int = 4
    max_watch: int = 2
    # LLM narrator: which provider phrases the brief. Empty/no key → template.
    narrator_provider: str = "deepseek"
    narrator_model: str = ""


@dataclass(frozen=True)
class AISettings:
    """AI debate-layer configuration.

    All three roles default to DeepSeek so a single DEEPSEEK_API_KEY is enough
    to run the full debate. Override individual roles via env vars if you want
    the multi-provider setup (e.g. Groq for bear, Hermes for arbiter).

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

    # Minimum reward:risk ratio to emit a signal (lower quality trades dropped).
    min_signal_rr: float = 1.5

    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    tracker: TrackerSettings = field(default_factory=TrackerSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    universe: UniverseSettings = field(default_factory=UniverseSettings)
    ai: AISettings = field(default_factory=AISettings)
    news: NewsSettings = field(default_factory=NewsSettings)
    reports: ReportsSettings = field(default_factory=ReportsSettings)
    flow: FlowSettings = field(default_factory=FlowSettings)
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
            interval_min=_env_int("FLOW_INTERVAL_MIN", 240),
            markets_limit=_env_int("FLOW_MARKETS_LIMIT", 60),
            max_picks=_env_int("FLOW_MAX_PICKS", 3),
            max_skips=_env_int("FLOW_MAX_SKIPS", 4),
            max_watch=_env_int("FLOW_MAX_WATCH", 2),
            narrator_provider=_env_str("FLOW_NARRATOR_PROVIDER", "deepseek"),
            narrator_model=_env_str("FLOW_NARRATOR_MODEL", ""),
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
        dedup_default = _env_int("TRACKER_DEDUP_MINUTES", 30)
        tracker = TrackerSettings(
            dedup_minutes=dedup_default,
            dedup_scalp_min=_env_int("TRACKER_DEDUP_SCALP_MIN", 10),
            dedup_prepump_min=_env_int("TRACKER_DEDUP_PREPUMP_MIN", 20),
            dedup_predump_min=_env_int("TRACKER_DEDUP_PREDUMP_MIN", 20),
            dedup_screener_min=_env_int("TRACKER_DEDUP_SCREENER_MIN", dedup_default),
            dedup_swing_min=_env_int("TRACKER_DEDUP_SWING_MIN", 60),
            max_outcomes=_env_int("TRACKER_MAX_OUTCOMES", 500),
            tp1_banks_win=_env_bool("TRACKER_TP1_BANKS_WIN", False),
        )
        risk = RiskSettings(
            regime_filter_enabled=_env_bool("REGIME_FILTER_ENABLED", True),
            regime_symbol=_env_str("REGIME_SYMBOL", "BTCUSDT"),
            regime_interval=_env_str("REGIME_INTERVAL", "1h"),
            drawdown_pause_pct=_env_float("DRAWDOWN_PAUSE_PCT", 15.0),
            autopause_min_trades=_env_int("AUTOPAUSE_MIN_TRADES", 12),
            autopause_min_expectancy=_env_float("AUTOPAUSE_MIN_EXPECTANCY", 0.10),
            autopause_min_win_rate=_env_float("AUTOPAUSE_MIN_WIN_RATE", 38.0),
            regime_hard_block=_env_bool("REGIME_HARD_BLOCK", False),
            autopause_hard_block=_env_bool("AUTOPAUSE_HARD_BLOCK", False),
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
                provider=_env_str("DEBATE_BEAR_PROVIDER", "groq"),
                model=_env_str("DEBATE_BEAR_MODEL", "llama-3.3-70b-versatile"),
            ),
            arbiter=DebateRole(
                provider=_env_str("DEBATE_ARBITER_PROVIDER", "hermes"),
                model=_env_str("DEBATE_ARBITER_MODEL", "nousresearch/hermes-3-llama-3.1-405b"),
            ),
            veto_enabled=_env_bool("AI_VETO_ENABLED", True),
            veto_min_confidence=_env_int("AI_VETO_MIN_CONFIDENCE", 70),
            chart_candles=_env_int("AI_CHART_CANDLES", 20),
        )
        exchanges = _env_csv("EXCHANGES") or ("binance", "okx", "bybit", "gate")
        return cls(
            state_dir=_env_str("STATE_DIR", "state_data"),
            paper_start_balance=_env_float("PAPER_START_BALANCE", 1000.0),
            paper_risk_pct=_env_float("PAPER_RISK_PCT", 1.0),
            http_timeout=_env_float("HTTP_TIMEOUT", 10.0),
            exchanges=tuple(exchanges),
            screener_interval_min=_env_int("SCREENER_INTERVAL_MIN", 10),
            tracker_interval_min=_env_int("TRACKER_INTERVAL_MIN", 5),
            stats_report_hours=_env_int("STATS_REPORT_HOURS", 24),
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
            min_signal_rr=_env_float("MIN_SIGNAL_RR", 1.5),
            telegram=telegram,
            tracker=tracker,
            risk=risk,
            universe=universe,
            ai=ai,
            news=news,
            reports=reports,
            flow=flow,
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
