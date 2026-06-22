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

from wolf.ai import DebateValidator, build_llm_client
from wolf.backtest import BacktestEngine
from wolf.config import Settings
from wolf.detectors import default_detectors
from wolf.exchange import (
    FUNDING_REGISTRY,
    SOURCE_REGISTRY,
    BinanceClient,
    MarketDataClient,
)
from wolf.learning import LearningEngine
from wolf.market import ContextProvider
from wolf.news import NewsService, build_news_source
from wolf.notify import TelegramNotifier
from wolf.reports import MajorsReporter, MarketPulse, MarketRadar, WhaleTracker
from wolf.risk import PaperTrader
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
    learning: Optional[LearningEngine] = None
    paper: Optional[PaperTrader] = None
    backtest: Optional[BacktestEngine] = None
    news: Optional[NewsService] = None
    majors: Optional[MajorsReporter] = None
    radar: Optional[MarketRadar] = None
    pulse: Optional[MarketPulse] = None
    whale: Optional[WhaleTracker] = None

    def warm_start_learning(self) -> None:
        """Seed learning memory from a backtest — only when memory is still empty.

        Best-effort and network-bound, so it is called from the worker entrypoint
        (not on every API construction) and never raised to the caller.
        """
        if not (self.backtest and self.learning and self.settings.backtest.warm_start):
            return
        if self.learning.snapshot().get("strategies"):
            return  # already have live/seeded history; don't double-count
        try:
            result = self.backtest.run(self.screener._universe)
            trades = [(t.strategy, t.symbol, t.pnl_pct, t.r_multiple) for t in result["trades"]]
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


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or Settings.from_env()

    store = StateStore(settings.state_dir)
    client = _build_market_client(settings)
    notifier = TelegramNotifier(
        settings.telegram, timeout=settings.http_timeout, tz=settings.timezone
    )

    learning = LearningEngine(store, settings.learning) if settings.learning.enabled else None
    paper = PaperTrader(store, settings.risk) if settings.risk.paper_enabled else None

    def on_event(sig, event, info):
        # On resolution, book the paper trade and update learning *before* the
        # notification so the resolution card can show R / USD / running edge.
        if event == "RESOLVED":
            info = dict(info)
            fill = paper.record(sig) if paper else None
            if fill:
                info.update(r=fill.r_multiple, pnl_usd=fill.pnl_usd, balance=fill.balance)
            if learning:
                learning.observe(sig, fill.r_multiple if fill else None)
                edge = learning.snapshot()["strategies"].get(sig.strategy)
                if edge:
                    info["edge"] = edge
        notifier.on_event(sig, event, info)

    tracker = Tracker(store, client, settings.tracker, notify=on_event)
    context_provider = ContextProvider(client)

    validator = None
    if settings.ai.enabled:
        # Pick the API key that matches the configured provider.
        provider_keys = {
            "anthropic": settings.anthropic_api_key,
            "deepseek": settings.deepseek_api_key,
        }
        api_key = provider_keys.get(settings.ai.provider, "")
        llm = build_llm_client(settings.ai.provider, api_key, settings.ai.model)
        validator = DebateValidator(llm)

    detectors = default_detectors()
    screener = Screener(
        client, tracker, detectors, notifier=notifier,
        context_provider=context_provider,
        validator=validator,
        veto_min_confidence=settings.ai.veto_min_confidence,
        learning=learning,
        regime=settings.regime,
    )
    backtest = BacktestEngine(
        client, detectors,
        lookback=settings.backtest.lookback,
        candle_limit=settings.backtest.candle_limit,
    )

    news = None
    if settings.news.enabled:
        source = build_news_source(settings.news.provider, timeout=settings.http_timeout)
        if source is not None:
            news = NewsService(source, store, max_items=settings.news.max_items)

    r = settings.reports
    tz = settings.timezone
    majors = MajorsReporter(client, tz=tz) if r.majors_enabled else None
    radar = MarketRadar(client, min_quote_volume=r.radar_min_quote_volume, tz=tz) if r.radar_enabled else None
    pulse = MarketPulse(client, tz=tz) if r.pulse_enabled else None
    whale = WhaleTracker(client, store, min_usd=r.whale_min_usd, tz=tz) if r.whale_enabled else None

    return Application(
        settings=settings,
        store=store,
        client=client,
        notifier=notifier,
        tracker=tracker,
        screener=screener,
        learning=learning,
        paper=paper,
        backtest=backtest,
        news=news,
        majors=majors,
        radar=radar,
        pulse=pulse,
        whale=whale,
    )
