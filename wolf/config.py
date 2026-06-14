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
    market_update_thread_id: str = ""
    trade_report_thread_id: str = ""
    news_thread_id: str = ""
    system_thread_id: str = ""   # startup / health / errors
    stats_thread_id: str = ""    # periodic performance summary
    whale_thread_id: str = ""    # 👁 Whale Report (large trades)
    radar_thread_id: str = ""    # 🔥 Hot Ecosystem (market radar)
    majors_thread_id: str = ""   # 🐝 BTC/ETH/SOL (majors session report)
    allowed_chat_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # ── routing with graceful fallback ──
    # Each message type goes to its own topic, falling back to the main channel
    # (empty thread id) when that topic isn't configured — so nothing is dropped.
    def route_new_signal(self) -> str:      # 🆕 New Signal
        return _first(self.new_signal_thread_id)

    def route_entry(self) -> str:           # ⭐ Signal Entry (activation / TP)
        return _first(self.signal_thread_id)

    def route_market_update(self) -> str:   # 📚 Market Update (bias/pulse)
        return _first(self.market_update_thread_id)

    def route_trade_report(self) -> str:    # 📝 Trade Reports (resolutions)
        return _first(self.trade_report_thread_id)

    def route_news(self) -> str:            # 🗞 News Update
        return _first(self.news_thread_id)

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
class TrackerSettings:
    """Signal-tracking behaviour knobs."""

    # Per signal-type timeout (hours) before a pending signal expires.
    timeout_screener_h: int = 24
    timeout_prepump_h: int = 12
    timeout_predump_h: int = 12
    timeout_scalp_h: int = 2
    timeout_swing_h: int = 24
    # Dedup window: skip an identical symbol+direction within this many minutes.
    dedup_minutes: int = 30
    # Keep at most this many resolved outcomes on disk.
    max_outcomes: int = 500

    def timeout_for(self, signal_type: str) -> int:
        return {
            "SCREENER": self.timeout_screener_h,
            "PREPUMP": self.timeout_prepump_h,
            "PREDUMP": self.timeout_predump_h,
            "SCALP": self.timeout_scalp_h,
            "SWING": self.timeout_swing_h,
        }.get(signal_type.upper(), self.timeout_screener_h)


@dataclass(frozen=True)
class NewsSettings:
    """Crypto-news posting configuration."""

    enabled: bool = False
    provider: str = "cryptocompare"  # free, key-less
    interval_min: int = 30
    max_items: int = 3


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
class AISettings:
    """AI debate-layer configuration.

    Each debate role runs on its own (cheap) provider so the layer costs a
    fraction of Claude: DeepSeek argues the bull case, Groq the bear case, and
    Hermes arbitrates. Any role can be repointed via env without code changes.
    """

    enabled: bool = False
    bull: DebateRole = DebateRole("deepseek", "deepseek-chat")
    bear: DebateRole = DebateRole("groq", "llama-3.3-70b-versatile")
    arbiter: DebateRole = DebateRole("hermes", "nousresearch/hermes-3-llama-3.1-405b")
    # If a REJECT verdict at/above this confidence should veto the signal.
    veto_enabled: bool = True
    veto_min_confidence: int = 70


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

    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    tracker: TrackerSettings = field(default_factory=TrackerSettings)
    ai: AISettings = field(default_factory=AISettings)
    news: NewsSettings = field(default_factory=NewsSettings)
    reports: ReportsSettings = field(default_factory=ReportsSettings)

    @classmethod
    def from_env(cls) -> "Settings":
        """Build a :class:`Settings` from the current environment."""
        telegram = TelegramSettings(
            bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
            chat_id=_env_str("TELEGRAM_CHAT_ID"),
            signal_thread_id=_env_str("SIGNAL_THREAD_ID"),
            new_signal_thread_id=_env_str("NEW_SIGNAL_THREAD_ID"),
            market_update_thread_id=_env_str("MARKET_UPDATE_THREAD_ID"),
            trade_report_thread_id=_env_str("TRADE_REPORT_THREAD_ID"),
            news_thread_id=_env_str("NEWS_THREAD_ID"),
            system_thread_id=_env_str("SYSTEM_THREAD_ID"),
            stats_thread_id=_env_str("STATS_THREAD_ID"),
            whale_thread_id=_env_str("WHALE_THREAD_ID"),
            radar_thread_id=_env_str("RADAR_THREAD_ID"),
            majors_thread_id=_env_str("MAJORS_THREAD_ID"),
            allowed_chat_ids=_env_csv("ALLOWED_CHAT_IDS"),
        )
        news = NewsSettings(
            enabled=_env_bool("NEWS_ENABLED", False),
            provider=_env_str("NEWS_PROVIDER", "cryptocompare"),
            interval_min=_env_int("NEWS_INTERVAL_MIN", 30),
            max_items=_env_int("NEWS_MAX_ITEMS", 3),
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
        tracker = TrackerSettings(
            dedup_minutes=_env_int("TRACKER_DEDUP_MINUTES", 30),
            max_outcomes=_env_int("TRACKER_MAX_OUTCOMES", 500),
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
            telegram=telegram,
            tracker=tracker,
            ai=ai,
            news=news,
            reports=reports,
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
            if f.name in ("telegram", "tracker", "ai", "news", "reports"):
                continue
            value = getattr(self, f.name)
            if f.name in secret_names or f.name.endswith("_anon_key"):
                out[f.name] = bool(value)  # only reveal presence
            else:
                out[f.name] = value
        out["telegram_enabled"] = self.telegram.enabled
        out["ai_enabled"] = self.ai.enabled
        out["news_enabled"] = self.news.enabled
        return out
