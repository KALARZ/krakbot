"""Kraken Spot REST client (block 2). See client.py for the safety properties."""

from krakbot.kraken_rest.client import IDEMPOTENT_PRIVATE, TRADING_METHODS, KrakenRestClient, RetryPolicy
from krakbot.kraken_rest.errors import (
    ErrorCategory,
    KrakenError,
    OutcomeUnknownError,
    TradingDisabledError,
    classify,
)
from krakbot.kraken_rest.models import AddOrderRequest, BookTop, Instrument, LocalValidationError
from krakbot.kraken_rest.nonce import InMemoryNonceStore, NonceSource, SqliteNonceStore

__all__ = [
    "IDEMPOTENT_PRIVATE", "TRADING_METHODS", "AddOrderRequest", "BookTop", "ErrorCategory", "InMemoryNonceStore",
    "Instrument", "KrakenError", "KrakenRestClient", "LocalValidationError", "NonceSource", "OutcomeUnknownError",
    "RetryPolicy", "SqliteNonceStore", "TradingDisabledError", "classify",
]
