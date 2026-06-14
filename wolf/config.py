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
    allowed_chat_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


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
class AISettings:
    """AI debate-layer configuration."""

    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-opus-4-8"
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

    # Scheduling (minutes)
    screener_interval_min: int = 10
    tracker_interval_min: int = 5

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
    newsapi_key: str = ""
    twitter_bearer_token: str = ""
    glassnode_api_key: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""

    log_level: str = "INFO"

    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    tracker: TrackerSettings = field(default_factory=TrackerSettings)
    ai: AISettings = field(default_factory=AISettings)

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
            allowed_chat_ids=_env_csv("ALLOWED_CHAT_IDS"),
        )
        tracker = TrackerSettings(
            dedup_minutes=_env_int("TRACKER_DEDUP_MINUTES", 30),
            max_outcomes=_env_int("TRACKER_MAX_OUTCOMES", 500),
        )
        ai = AISettings(
            enabled=_env_bool("AI_DEBATE_ENABLED", False),
            provider=_env_str("AI_PROVIDER", "anthropic"),
            model=_env_str("CLAUDE_MODEL", "claude-opus-4-8"),
            veto_enabled=_env_bool("AI_VETO_ENABLED", True),
            veto_min_confidence=_env_int("AI_VETO_MIN_CONFIDENCE", 70),
        )
        exchanges = _env_csv("EXCHANGES") or ("binance", "okx", "bybit", "gate")
        return cls(
            state_dir=_env_str("STATE_DIR", "state_data"),
            http_timeout=_env_float("HTTP_TIMEOUT", 10.0),
            exchanges=tuple(exchanges),
            screener_interval_min=_env_int("SCREENER_INTERVAL_MIN", 10),
            tracker_interval_min=_env_int("TRACKER_INTERVAL_MIN", 5),
            api_host=_env_str("API_HOST", "0.0.0.0"),
            api_port=_env_int("API_PORT", 8000),
            api_key=_env_str("API_KEY"),
            gemini_api_key=_env_str("GEMINI_API_KEY"),
            groq_api_key=_env_str("GROQ_API_KEY"),
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY"),
            deepseek_api_key=_env_str("DEEPSEEK_API_KEY"),
            newsapi_key=_env_str("NEWSAPI_KEY"),
            twitter_bearer_token=_env_str("TWITTER_BEARER_TOKEN"),
            glassnode_api_key=_env_str("GLASSNODE_API_KEY"),
            supabase_url=_env_str("SUPABASE_URL"),
            supabase_anon_key=_env_str("SUPABASE_ANON_KEY"),
            log_level=_env_str("LOG_LEVEL", "INFO"),
            telegram=telegram,
            tracker=tracker,
            ai=ai,
        )

    def describe(self) -> dict:
        """Return a redacted, JSON-serialisable view for diagnostics/health."""
        secret_names = {f.name for f in fields(self) if f.name.endswith(("_key", "_token"))}
        out: dict = {}
        for f in fields(self):
            if f.name in ("telegram", "tracker", "ai"):
                continue
            value = getattr(self, f.name)
            if f.name in secret_names or f.name.endswith("_anon_key"):
                out[f.name] = bool(value)  # only reveal presence
            else:
                out[f.name] = value
        out["telegram_enabled"] = self.telegram.enabled
        out["ai_enabled"] = self.ai.enabled
        return out
