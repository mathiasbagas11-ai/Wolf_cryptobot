"""Application composition root.

Builds the object graph (store, exchange client, tracker, detectors, notifier,
screener) from a single :class:`~wolf.config.Settings`. Wiring lives here and
nowhere else, so both the worker entrypoint and the API share one consistent
set of components and there is no module-level global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from wolf.ai import DebateValidator, build_llm_client
from wolf.config import Settings
from wolf.detectors import default_detectors
from wolf.exchange import (
    FUNDING_REGISTRY,
    SOURCE_REGISTRY,
    BinanceClient,
    MarketDataClient,
)
from wolf.market import ContextProvider
from wolf.news import NewsService, build_news_source
from wolf.notify import TelegramNotifier
from wolf.reports import MajorsReporter, MarketPulse, MarketRadar, WhaleTracker
from wolf.screener import Screener
from wolf.state import StateStore
from wolf.tracker import Tracker


@dataclass
class Application:
    settings: Settings
    store: StateStore
    client: MarketDataClient
    notifier: TelegramNotifier
    tracker: Tracker
    screener: Screener
    news: Optional[NewsService] = None
    majors: Optional[MajorsReporter] = None
    radar: Optional[MarketRadar] = None
    pulse: Optional[MarketPulse] = None
    whale: Optional[WhaleTracker] = None


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
    tracker = Tracker(store, client, settings.tracker, notify=notifier.on_event)
    context_provider = ContextProvider(client)

    validator = None
    if settings.ai.enabled:
        llm = build_llm_client(
            settings.ai.provider, settings.anthropic_api_key, settings.ai.model
        )
        validator = DebateValidator(llm)

    screener = Screener(
        client, tracker, default_detectors(), notifier=notifier,
        context_provider=context_provider,
        validator=validator,
        veto_min_confidence=settings.ai.veto_min_confidence,
        regime_hard_block=settings.risk.regime_hard_block,
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
        news=news,
        majors=majors,
        radar=radar,
        pulse=pulse,
        whale=whale,
    )
