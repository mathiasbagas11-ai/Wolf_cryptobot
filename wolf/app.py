"""Application composition root.

Builds the object graph (store, exchange client, tracker, detectors, notifier,
screener) from a single :class:`~wolf.config.Settings`. Wiring lives here and
nowhere else, so both the worker entrypoint and the API share one consistent
set of components and there is no module-level global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import logging
import os

from wolf.account import PaperAccount
from wolf.ai import DebateValidator, build_llm_client
from wolf.analyze import AnalyzeService
from wolf.backtest import BacktestEngine
from wolf.config import Settings
from wolf.detectors import default_detectors
from wolf.learning import LearningEngine
from wolf.exchange import (
    FUNDING_REGISTRY,
    SOURCE_REGISTRY,
    BinanceClient,
    MarketDataClient,
)
from wolf.market import ContextProvider
from wolf.regime import RegimeProvider
from wolf.universe import UniverseProvider
from wolf.news import NewsService, NewsSynthesizer, build_news_source
from wolf.news.signal import NewsSignalScanner
from wolf.notify import TelegramNotifier
from wolf.reports import (
    FlowReporter,
    MajorsReporter,
    MarketPulse,
    MarketRadar,
    WhaleTracker,
)
from wolf.screener import Screener
from wolf.state import StateStore
from wolf.tracker import Tracker

log = logging.getLogger("wolf.app")


@dataclass
class Application:
    settings: Settings
    store: StateStore
    client: MarketDataClient
    notifier: TelegramNotifier
    tracker: Tracker
    screener: Screener
    account: Optional[PaperAccount] = None
    learning: Optional[LearningEngine] = None
    backtest: Optional[BacktestEngine] = None
    analyze: Optional[AnalyzeService] = None
    news: Optional[NewsService] = None
    news_synth: Optional[NewsSynthesizer] = None
    news_scanner: Optional[NewsSignalScanner] = None
    majors: Optional[MajorsReporter] = None
    radar: Optional[MarketRadar] = None
    pulse: Optional[MarketPulse] = None
    whale: Optional[WhaleTracker] = None
    flow: Optional[FlowReporter] = None
    anomaly: object = None            # anomaly.scanner.AnomalyScanner (optional)

    def warm_start_learning(self) -> None:
        """Seed learning memory from a backtest — only when memory is empty.

        Best-effort and network-bound, so it is called from the worker entrypoint
        (not on every API construction) and never raised to the caller.
        """
        if not (self.backtest and self.learning and self.settings.backtest.warm_start):
            return
        if self.learning.snapshot().get("strategies"):
            return  # already have live/seeded history; don't double-count
        try:
            result = self.backtest.run(self.screener.current_universe())
            trades = [(t.strategy, t.symbol, t.win, t.pnl_pct, t.r_multiple) for t in result["trades"]]
            self.learning.seed(trades)
            log.info("Warm-started learning from %d backtested trades", len(trades))
        except Exception:
            log.exception("Backtest warm-start failed (non-fatal)")


def _build_market_client(settings: Settings) -> MarketDataClient:
    """Compose the ordered fallback sources + a Binance futures provider."""
    sources = []
    for name in settings.exchanges:
        factory = SOURCE_REGISTRY.get(name)
        if factory is None:
            continue
        # Binance source reuses the configured spot base; others use defaults.
        if name == "binance":
            sources.append(factory(base_url=settings.binance_spot_base, timeout=settings.http_timeout))
        else:
            sources.append(factory(timeout=settings.http_timeout))
    if not sources:  # never leave the client without a source
        from wolf.exchange import BinanceSource

        sources.append(BinanceSource(base_url=settings.binance_spot_base, timeout=settings.http_timeout))

    # Funding sources follow the same venue order; only venues that expose
    # funding appear in FUNDING_REGISTRY.
    funding_sources = [
        FUNDING_REGISTRY[name](timeout=settings.http_timeout)
        for name in settings.exchanges
        if name in FUNDING_REGISTRY
    ]
    futures = BinanceClient(
        spot_base=settings.binance_spot_base,
        futures_base=settings.binance_futures_base,
        timeout=settings.http_timeout,
    )
    return MarketDataClient(sources, futures=futures, funding_sources=funding_sources)


def ai_status(app: "Application") -> dict:
    """Whether the debate layer can actually return a verdict, and what is missing.

    ``enabled`` is intent, ``available`` is reality. They disagree whenever the
    arbiter has no usable client, which produces an unbroken run of ABSTAIN
    verdicts that the stats cannot tell apart from genuine abstentions.
    """
    validator = getattr(app.screener, "_validator", None)
    if validator is None:
        return {
            "enabled": app.settings.ai.enabled,
            "available": False,
            "degraded_roles": [],
        }
    return {
        "enabled": app.settings.ai.enabled,
        "available": bool(getattr(validator, "available", False)),
        "degraded_roles": list(getattr(validator, "degraded_roles", [])),
    }


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or Settings.from_env()

    store = StateStore(settings.state_dir)
    # Log where state actually landed and how much history survived. On Railway a
    # relative STATE_DIR resolves inside the container filesystem, which is
    # discarded on every redeploy — so "0 outcomes" right after a deploy that had
    # hundreds means the sample was wiped, not that trading went quiet.
    if not os.path.isabs(settings.state_dir):
        log.warning(
            "STATE_DIR=%r is relative -> %s. On an ephemeral filesystem every "
            "redeploy discards signal history. Mount a volume and point STATE_DIR at it.",
            settings.state_dir, store.base_dir,
        )
    log.info(
        "State dir %s (%d resolved outcomes on disk)",
        store.base_dir, len(store.read("signal_outcomes", default=[]) or []),
    )
    client = _build_market_client(settings)
    account = PaperAccount(
        store,
        start_balance=settings.paper_start_balance,
        risk_pct=settings.paper_risk_pct,
    )
    notifier = TelegramNotifier(
        settings.telegram, timeout=settings.http_timeout, tz=settings.timezone,
        risk=settings.risk, account=account,
        risk_pct=settings.paper_risk_pct, start_balance=settings.paper_start_balance,
        ladder=settings.ladder,
    )
    learning = LearningEngine(store, settings.learning) if settings.learning.enabled else None
    tracker = Tracker(
        store, client, settings.tracker, notify=notifier.on_event,
        account=account, learning=learning, ladder=settings.ladder,
    )
    context_provider = ContextProvider(client)

    def _role_client(role, label: str = ""):
        llm = build_llm_client(
            role.provider, settings.api_key_for(role.provider), role.model
        )
        if label and not llm.available:
            # An enabled-but-unusable client degrades to a permanent ABSTAIN,
            # which in the stats is indistinguishable from "the AI looked and
            # had no opinion". Two days of 100% ABSTAIN went unnoticed that way.
            log.warning(
                "AI role %s is enabled (provider=%s) but its client is unavailable "
                "— missing API key or unsupported provider. It will ABSTAIN on "
                "every signal and filter nothing.",
                label, role.provider,
            )
        return llm

    validator = None
    analysis_llm = None
    if settings.ai.enabled:
        validator = DebateValidator(
            bull=_role_client(settings.ai.bull, "bull"),
            bear=_role_client(settings.ai.bear, "bear"),
            arbiter=_role_client(settings.ai.arbiter, "arbiter"),
            chart_candles=settings.ai.chart_candles,
        )
        # The arbiter alone returns the structured verdict, so without it the
        # layer cannot decide anything — it abstains on every signal while
        # looking configured. Say which roles are live, at boot, either way.
        if not validator.available:
            log.error(
                "AI debate is ENABLED but the arbiter (provider=%s) has no usable "
                "client — every signal will ABSTAIN. Set the arbiter provider's API "
                "key, or point DEBATE_ARBITER_PROVIDER at one you have.",
                settings.ai.arbiter.provider,
            )
        else:
            log.info(
                "AI debate ready — bull=%s bear=%s arbiter=%s%s",
                settings.ai.bull.provider, settings.ai.bear.provider,
                settings.ai.arbiter.provider,
                f" (no client for: {', '.join(validator.degraded_roles)})"
                if validator.degraded_roles else "",
            )
        # Reuse the (cheap) arbiter model to narrate market/session reports.
        analysis_llm = _role_client(settings.ai.arbiter)

    regime_provider = RegimeProvider(
        client, symbol=settings.risk.regime_symbol, interval=settings.risk.regime_interval
    )
    macro_provider = None
    if settings.risk.composite_regime_enabled:
        from wolf.flow import CoinGeckoClient, DefiLlamaClient, SentimentClient
        from wolf.regime_composite import CompositeRegimeProvider

        rk = settings.risk
        macro_provider = CompositeRegimeProvider(
            regime_provider,
            sentiment_client=SentimentClient(timeout=settings.http_timeout),
            coingecko_client=CoinGeckoClient(timeout=settings.http_timeout),
            defillama_client=DefiLlamaClient(timeout=settings.http_timeout),
            store=store,
            fear_extreme_max=rk.fear_extreme_max,
            usdtd_change_pct=rk.usdtd_riskoff_change_pct,
            usdtd_reversal_percentile=rk.usdtd_reversal_percentile,
            usdtd_history_days=rk.usdtd_history_days,
            usdtd_min_history_days=rk.usdtd_min_history_days,
            dry_powder_outflow_pct=rk.dry_powder_outflow_pct,
            ttl_min=rk.flow_context_ttl_min,
        )
    universe_provider = (
        UniverseProvider(
            client,
            top_n=settings.universe.top_n,
            min_quote_volume=settings.universe.min_quote_volume,
            quote=settings.universe.quote,
        )
        if settings.universe.dynamic
        else None
    )
    detectors = default_detectors(ladder=settings.ladder, flow_veto=settings.flow_veto)
    screener = Screener(
        client, tracker, detectors, notifier=notifier,
        context_provider=context_provider,
        validator=validator,
        veto_min_confidence=settings.ai.veto_min_confidence,
        regime_provider=regime_provider,
        macro_provider=macro_provider,
        account=account,
        risk=settings.risk,
        universe_provider=universe_provider,
        min_rr=settings.min_signal_rr,
        round_trip_bps=settings.round_trip_cost_bps,
        max_cost_r=settings.max_cost_r,
        learning=learning,
    )
    backtest = BacktestEngine(
        client, detectors,
        lookback=settings.backtest.lookback,
        candle_limit=settings.backtest.candle_limit,
    )
    analyze = AnalyzeService(
        client, detectors,
        context_provider=context_provider,
        learning=learning,
        validator=validator,
        tz=settings.timezone,
    )

    news = None
    news_synth = None
    if settings.news.enabled:
        source = build_news_source(settings.news.sources, timeout=settings.http_timeout)
        if source is not None:
            news = NewsService(source, store, max_items=settings.news.max_items)
        if settings.news.synthesis_enabled:
            n = settings.news
            narrator = build_llm_client(
                n.narrator_provider, settings.api_key_for(n.narrator_provider), n.narrator_model
            )
            news_synth = NewsSynthesizer(narrator)

    news_scanner = None
    if settings.news.signals_enabled and news is not None:
        news_scanner = NewsSignalScanner(client, universe=set(screener.current_universe()))

    r = settings.reports
    tz = settings.timezone
    majors = MajorsReporter(client, tz=tz, llm=analysis_llm) if r.majors_enabled else None
    radar = MarketRadar(client, min_quote_volume=r.radar_min_quote_volume, tz=tz) if r.radar_enabled else None
    pulse = MarketPulse(client, tz=tz, llm=analysis_llm) if r.pulse_enabled else None
    whale = WhaleTracker(client, store, min_usd=r.whale_min_usd, tz=tz) if r.whale_enabled else None

    anomaly = build_anomaly_scanner(settings) if settings.anomaly.enabled else None
    flow = build_flow_reporter(settings, client, anomaly=anomaly) if settings.flow.enabled else None

    return Application(
        settings=settings,
        store=store,
        client=client,
        notifier=notifier,
        tracker=tracker,
        screener=screener,
        account=account,
        learning=learning,
        backtest=backtest,
        analyze=analyze,
        news=news,
        news_synth=news_synth,
        news_scanner=news_scanner,
        majors=majors,
        radar=radar,
        pulse=pulse,
        whale=whale,
        flow=flow,
        anomaly=anomaly,
    )


def build_anomaly_scanner(settings: Settings):
    """Construct the anomaly scanner (PAPER MODE), wiring the optional Google-Sheet
    paper logger from the deployment's service-account credentials. A Sheets
    problem is swallowed so the scanner still runs (and posts) without logging."""
    from anomaly.scanner import AnomalyScanner

    a = settings.anomaly
    paper_logger = None
    if a.paper_log_enabled and a.sheets_credentials:
        try:
            from anomaly.logger import AnomalyPaperLogger, open_worksheet
            ws = open_worksheet(a.sheets_credentials, a.sheet_name)
            paper_logger = AnomalyPaperLogger(ws, min_score=a.min_score)
        except Exception:  # never break boot on a Sheets/gspread issue
            log.exception("Anomaly paper logger unavailable — scanning without it")

    return AnomalyScanner(
        min_score=a.min_score,
        max_picks=a.max_picks,
        scan_limit=a.scan_limit,
        time_budget_sec=a.time_budget_sec,
        paper_mode=a.paper_mode,
        logger=paper_logger,
    )


def build_flow_reporter(settings: Settings, client: MarketDataClient,
                        anomaly=None) -> FlowReporter:
    """Construct the flow-intelligence reporter (used by the scheduler and the
    on-demand REST endpoints, so a deep-dive works even when scheduling is off)."""
    from wolf.flow import (
        CoinGeckoClient,
        DefiLlamaClient,
        HyperliquidPerps,
        SentimentClient,
    )

    f = settings.flow
    narrator = build_llm_client(
        f.narrator_provider, settings.api_key_for(f.narrator_provider), f.narrator_model
    )
    return FlowReporter(
        coingecko=CoinGeckoClient(timeout=settings.http_timeout),
        defillama=DefiLlamaClient(timeout=settings.http_timeout),
        sentiment=SentimentClient(timeout=settings.http_timeout),
        hyperliquid=HyperliquidPerps(timeout=settings.http_timeout),
        narrator=narrator,
        market_client=client,
        anomaly=anomaly,
        markets_limit=f.markets_limit,
        max_picks=f.max_picks,
        max_skips=f.max_skips,
        max_watch=f.max_watch,
        tz=settings.timezone,
    )


