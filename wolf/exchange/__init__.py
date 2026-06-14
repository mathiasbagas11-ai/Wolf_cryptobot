from wolf.exchange.binance import BinanceClient
from wolf.exchange.client import MarketDataClient
from wolf.exchange.derivatives import (
    BinanceFunding,
    BybitFunding,
    FundingSource,
    OKXFunding,
)
from wolf.exchange.sources import (
    BinanceSource,
    BybitSource,
    ExchangeSource,
    GateSource,
    OKXSource,
)

#: Registry of available klines/price sources by name (env-driven selection).
SOURCE_REGISTRY = {
    "binance": BinanceSource,
    "okx": OKXSource,
    "bybit": BybitSource,
    "gate": GateSource,
}

#: Registry of venues that also provide a funding rate.
FUNDING_REGISTRY = {
    "binance": BinanceFunding,
    "okx": OKXFunding,
    "bybit": BybitFunding,
}

__all__ = [
    "BinanceClient",
    "MarketDataClient",
    "ExchangeSource",
    "BinanceSource",
    "OKXSource",
    "BybitSource",
    "GateSource",
    "FundingSource",
    "BinanceFunding",
    "OKXFunding",
    "BybitFunding",
    "SOURCE_REGISTRY",
    "FUNDING_REGISTRY",
]
