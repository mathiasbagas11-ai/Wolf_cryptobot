"""Application composition root.

Builds the object graph (store, exchange client, tracker, detectors, notifier,
screener) from a single :class:`~wolf.config.Settings`. Wiring lives here and
nowhere else, so both the worker entrypoint and the API share one consistent
set of components and there is no module-level global state.
"""

from __future__ import annotations

from dataclasses import dataclass

from wolf.ai import DebateValidator, build_llm_client
from wolf.config import Settings
from wolf.detectors import default_detectors
from wolf.exchange import BinanceClient
from wolf.market import ContextProvider
from wolf.notify import TelegramNotifier
from wolf.screener import Screener
from wolf.state import StateStore
from wolf.tracker import Tracker


@dataclass
class Application:
    settings: Settings
    store: StateStore
    client: BinanceClient
    notifier: TelegramNotifier
    tracker: Tracker
    screener: Screener


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or Settings.from_env()

    store = StateStore(settings.state_dir)
    client = BinanceClient(
        spot_base=settings.binance_spot_base,
        futures_base=settings.binance_futures_base,
        timeout=settings.http_timeout,
    )
    notifier = TelegramNotifier(settings.telegram, timeout=settings.http_timeout)
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
    )

    return Application(
        settings=settings,
        store=store,
        client=client,
        notifier=notifier,
        tracker=tracker,
        screener=screener,
    )
