from wolf.exchange.binance import BinanceClient
from wolf.exchange.client import MarketDataClient
from wolf.exchange.sources import (
    BinanceSource,
    BybitSource,
    ExchangeSource,
    OKXSource,
)

#: Registry of available sources by name, for env-driven selection.
SOURCE_REGISTRY = {
    "binance": BinanceSource,
    "okx": OKXSource,
    "bybit": BybitSource,
}

__all__ = [
    "BinanceClient",
    "MarketDataClient",
    "ExchangeSource",
    "BinanceSource",
    "OKXSource",
    "BybitSource",
    "SOURCE_REGISTRY",
]
