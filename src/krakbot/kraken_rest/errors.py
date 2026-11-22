"""Kraken error taxonomy and classification (docs: exchange/guides/general/errors).

The category decides what the client may do automatically:
  RETRYABLE / RATE_LIMIT / INVALID_NONCE: the request was not executed -> safe to retry with backoff
  UNKNOWN_OUTCOME: the request may have been executed (deadline elapsed / timeout after send) -> never resend
                   blindly; the caller must resolve via OpenOrders/ClosedOrders (plan §4.4)
  everything else: do not retry.
"""

from __future__ import annotations

import re
from enum import StrEnum

__all__ = [
    "AuthError", "ErrorCategory", "InsufficientFundsError", "KrakenError", "MarketStateError", "OrderRejectedError",
    "OutcomeUnknownError", "PermissionDeniedError", "RateLimitError", "TradingDisabledError", "classify", "error_for",
]


class ErrorCategory(StrEnum):
    RETRYABLE = "retryable"
    RATE_LIMIT = "rate_limit"
    INVALID_NONCE = "invalid_nonce"
    UNKNOWN_OUTCOME = "unknown_outcome"
    AUTH = "auth"
    PERMISSION = "permission"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    ORDER_REJECTED = "order_rejected"
    MARKET_STATE = "market_state"
    UNKNOWN_ORDER = "unknown_order"
    OTHER = "other"


class KrakenError(Exception):
    category = ErrorCategory.OTHER

    def __init__(self, method: str, errors: list[str], *, retry_after_s: float | None = None) -> None:
        self.method = method
        self.errors = errors
        self.retry_after_s = retry_after_s
        super().__init__(f"{method}: {', '.join(errors)}")


class RetryableError(KrakenError):
    category = ErrorCategory.RETRYABLE


class RateLimitError(KrakenError):
    category = ErrorCategory.RATE_LIMIT


class InvalidNonceError(KrakenError):
    category = ErrorCategory.INVALID_NONCE


class OutcomeUnknownError(KrakenError):
    category = ErrorCategory.UNKNOWN_OUTCOME


class AuthError(KrakenError):
    category = ErrorCategory.AUTH


class PermissionDeniedError(KrakenError):
    category = ErrorCategory.PERMISSION


class InsufficientFundsError(KrakenError):
    category = ErrorCategory.INSUFFICIENT_FUNDS


class OrderRejectedError(KrakenError):
    category = ErrorCategory.ORDER_REJECTED


class MarketStateError(KrakenError):
    category = ErrorCategory.MARKET_STATE


class UnknownOrderError(KrakenError):
    category = ErrorCategory.UNKNOWN_ORDER


class TradingDisabledError(Exception):
    """Raised BEFORE any network I/O when a trading endpoint is called on a client built without trading."""


_RULES: list[tuple[str, ErrorCategory]] = [
    (r"^EService:Deadline elapsed", ErrorCategory.UNKNOWN_OUTCOME),
    (r"^EAPI:Invalid nonce", ErrorCategory.INVALID_NONCE),
    (r"^(EAPI|EOrder):Rate limit exceeded|^EService:Throttled|^EOrder:Domain rate limit exceeded"
     r"|^EGeneral:Too many requests", ErrorCategory.RATE_LIMIT),
    (r"^EService:(Unavailable|Busy)|^EGeneral:Internal error|^EService:Timeout", ErrorCategory.RETRYABLE),
    (r"^EAPI:(Invalid key|Invalid signature|Bad request)|^EGeneral:Temporary lockout", ErrorCategory.AUTH),
    (r"^EGeneral:Permission denied|^EAPI:Feature disabled", ErrorCategory.PERMISSION),
    (r"^EOrder:Insufficient funds|^EFunding:Insufficient funds", ErrorCategory.INSUFFICIENT_FUNDS),
    (r"^EService:Market in (cancel_only|post_only|limit_only) mode|^EService:Market in maintenance",
     ErrorCategory.MARKET_STATE),
    (r"^EOrder:Unknown order", ErrorCategory.UNKNOWN_ORDER),
    (r"^EOrder:|^EGeneral:Invalid arguments|^EQuery:Unknown asset pair", ErrorCategory.ORDER_REJECTED),
]
_CLASSES: dict[ErrorCategory, type[KrakenError]] = {
    ErrorCategory.RETRYABLE: RetryableError, ErrorCategory.RATE_LIMIT: RateLimitError,
    ErrorCategory.INVALID_NONCE: InvalidNonceError, ErrorCategory.UNKNOWN_OUTCOME: OutcomeUnknownError,
    ErrorCategory.AUTH: AuthError, ErrorCategory.PERMISSION: PermissionDeniedError,
    ErrorCategory.INSUFFICIENT_FUNDS: InsufficientFundsError, ErrorCategory.ORDER_REJECTED: OrderRejectedError,
    ErrorCategory.MARKET_STATE: MarketStateError, ErrorCategory.UNKNOWN_ORDER: UnknownOrderError,
    ErrorCategory.OTHER: KrakenError,
}
_PRIORITY = [ErrorCategory.UNKNOWN_OUTCOME, ErrorCategory.AUTH, ErrorCategory.PERMISSION, ErrorCategory.INVALID_NONCE,
             ErrorCategory.RATE_LIMIT, ErrorCategory.MARKET_STATE, ErrorCategory.INSUFFICIENT_FUNDS,
             ErrorCategory.UNKNOWN_ORDER, ErrorCategory.ORDER_REJECTED, ErrorCategory.RETRYABLE, ErrorCategory.OTHER]
_THROTTLED = re.compile(r"^EService:\s*Throttled:\s*\[?(\d+(?:\.\d+)?)\]?")


def classify(errors: list[str]) -> ErrorCategory:
    """Most severe category among the error strings (UNKNOWN_OUTCOME dominates: never resend)."""
    found = set()
    for e in errors:
        for pattern, cat in _RULES:
            if re.search(pattern, e.strip()):
                found.add(cat)
                break
        else:
            found.add(ErrorCategory.OTHER)
    for cat in _PRIORITY:
        if cat in found:
            return cat
    return ErrorCategory.OTHER


def error_for(method: str, errors: list[str], now_s: float) -> KrakenError:
    cat = classify(errors)
    retry_after = None
    for e in errors:
        m = _THROTTLED.match(e.strip())
        if m:
            retry_after = max(0.0, float(m.group(1)) - now_s)
    return _CLASSES[cat](method, errors, retry_after_s=retry_after)
