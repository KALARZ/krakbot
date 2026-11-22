"""Signed async client for the Kraken Spot REST API (docs.kraken.com/api-reference).

Safety properties (tested in tests/unit/test_kraken_rest.py):
* trading endpoints raise TradingDisabledError before any I/O unless the client was built with trading_enabled=True
  (the bot only does that for a strategy in LIVE mode after the live gate) -> DRY_RUN can never send an order;
* private requests are serialized: nonce generation + send happen under one lock, so nonces reach Kraken in order;
* the nonce high-water mark is persisted before the request is sent;
* automatic retries only when the request was certainly not executed (connect errors, rate limits, invalid nonce)
  or the call is idempotent; a non-idempotent call whose outcome is ambiguous raises OutcomeUnknownError;
* credentials never appear in repr(), exceptions or log records.
"""

from __future__ import annotations

import asyncio
import logging
import random
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from krakbot.kraken_rest.clock import Clock, SystemClock
from krakbot.kraken_rest.errors import (
    ErrorCategory,
    KrakenError,
    OutcomeUnknownError,
    RateLimitError,
    TradingDisabledError,
    error_for,
)
from krakbot.kraken_rest.models import (
    AddOrderRequest,
    BookTop,
    Instrument,
    LocalValidationError,
    merge_display_symbols,
    parse_asset_pairs,
    parse_ticker,
)
from krakbot.kraken_rest.nonce import NonceSource
from krakbot.kraken_rest.ratelimit import EngineCounter, PublicLimiter, RestCallCounter, Tier, engine_cost
from krakbot.kraken_rest.signing import decode_secret, sign
from krakbot.numeric import to_db

__all__ = ["IDEMPOTENT_PRIVATE", "KRAKEN_API_URL", "TRADING_METHODS", "KrakenRestClient", "RetryPolicy"]

KRAKEN_API_URL = "https://api.kraken.com"

log = logging.getLogger("krakbot.kraken_rest")

TRADING_METHODS = frozenset({
    "AddOrder", "AddOrderBatch", "AmendOrder", "EditOrder", "CancelOrder", "CancelOrderBatch", "CancelAll",
    "CancelAllOrdersAfter",
})
IDEMPOTENT_PRIVATE = frozenset({"Balance", "BalanceEx", "TradeBalance", "OpenOrders", "ClosedOrders", "QueryOrders",
                                "TradesHistory", "QueryTrades", "TradeVolume", "GetWebSocketsToken", "CancelOrder",
                                "CancelAll", "CancelAllOrdersAfter", "Ledgers", "QueryLedgers"})
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)  # the request never left this host: always safe to retry
_AMBIGUOUS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_s: float = 1.0
    cap_s: float = 60.0
    rng: random.Random = field(default_factory=random.Random)

    def backoff(self, attempt: int) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**attempt))."""
        return self.rng.uniform(0.0, min(self.cap_s, self.base_s * (2**attempt)))


class KrakenRestClient:
    def __init__(self, *, http: httpx.AsyncClient, api_key: SecretStr | None = None,
                 api_secret: SecretStr | None = None, nonce: NonceSource | None = None,
                 trading_enabled: bool = False, tier: Tier = "starter", retry: RetryPolicy | None = None,
                 clock: Clock | None = None, base_url: str = KRAKEN_API_URL) -> None:
        self._http = http
        self._base = base_url.rstrip("/")  # absolute URLs: works whether or not `http` has a base_url
        self._clock = clock or SystemClock()
        self._key = api_key
        self._secret = decode_secret(api_secret.get_secret_value()) if api_secret is not None else None
        self._nonce = nonce
        self._trading = trading_enabled
        self._retry = retry or RetryPolicy()
        self._rest = RestCallCounter(tier, self._clock)
        self._engine = EngineCounter(tier, self._clock)
        self._public = PublicLimiter(self._clock)
        self._private_lock: asyncio.Lock | None = None  # created lazily inside the running loop
        if trading_enabled and (api_key is None or api_secret is None or nonce is None):
            raise ValueError("trading requires API key, secret and a persisted nonce source")

    def __repr__(self) -> str:
        return f"KrakenRestClient(trading_enabled={self._trading}, has_credentials={self._key is not None})"

    @property
    def trading_enabled(self) -> bool:
        return self._trading

    # ------------------------------------------------------------------ transport
    async def _public_call(self, method: str, params: Mapping[str, str] | None = None) -> Any:
        async def send() -> httpx.Response:
            await self._public.acquire()
            return await self._http.get(f"{self._base}/0/public/{method}", params=dict(params or {}))
        return await self._with_retries(method, send, idempotent=True)

    async def _private_call(self, method: str, data: Sequence[tuple[str, str]] = (), *, engine_pair: str | None = None,
                            engine_units: int = 0) -> Any:
        if method in TRADING_METHODS and not self._trading:
            raise TradingDisabledError(f"{method} refused: client built without trading (DRY_RUN or no live gate)")
        if self._key is None or self._secret is None or self._nonce is None:
            raise KrakenError(method, ["ECLIENT:missing API credentials or nonce source"])
        idempotent = method in IDEMPOTENT_PRIVATE
        if self._private_lock is None:
            self._private_lock = asyncio.Lock()
        path = f"/0/private/{method}"

        async def send() -> httpx.Response:
            assert self._key is not None and self._secret is not None and self._nonce is not None
            await self._rest.acquire(method)
            if engine_pair is not None and engine_units > 0:
                await self._engine.acquire(engine_pair, engine_units)
            assert self._private_lock is not None
            async with self._private_lock:
                nonce = self._nonce.next()
                body = urllib.parse.urlencode([("nonce", str(nonce)), *data])
                headers = {"API-Key": self._key.get_secret_value(), "API-Sign": sign(path, nonce, body, self._secret),
                           "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
                return await self._http.post(self._base + path, content=body.encode(), headers=headers)
        return await self._with_retries(method, send, idempotent=idempotent)

    async def _with_retries(self, method: str, send: Any, *, idempotent: bool) -> Any:  # noqa: PLR0915
        # one linear state machine on purpose: every exit path is visible and covered by a test
        last: Exception | None = None
        nonce_retries = 0
        for attempt in range(self._retry.max_attempts):
            try:
                resp = await send()
            except _NOT_SENT as exc:
                last = KrakenError(method, [f"ECLIENT:connect failed ({type(exc).__name__})"])
                await self._sleep(attempt, method, "connect")
                continue
            except _AMBIGUOUS as exc:
                if not idempotent:
                    raise OutcomeUnknownError(method, [f"ECLIENT:{type(exc).__name__} after send"]) from None
                last = KrakenError(method, [f"ECLIENT:{type(exc).__name__}"])
                await self._sleep(attempt, method, "transport")
                continue
            if resp.status_code == 429:
                last = RateLimitError(method, ["EHTTP:429"])
                await self._sleep(attempt, method, "http429")
                continue
            if resp.status_code >= 500:
                if not idempotent:
                    raise OutcomeUnknownError(method, [f"EHTTP:{resp.status_code}"])
                last = KrakenError(method, [f"EHTTP:{resp.status_code}"])
                await self._sleep(attempt, method, "http5xx")
                continue
            if resp.status_code != 200:
                raise KrakenError(method, [f"EHTTP:{resp.status_code}"])
            try:
                payload = resp.json()
                errors = [str(e) for e in payload.get("error", []) if str(e).startswith("E")]
                warnings = [str(e) for e in payload.get("error", []) if str(e).startswith("W")]
            except (ValueError, AttributeError):
                if not idempotent:
                    raise OutcomeUnknownError(method, ["ECLIENT:unparseable response"]) from None
                last = KrakenError(method, ["ECLIENT:unparseable response"])
                await self._sleep(attempt, method, "decode")
                continue
            for w in warnings:
                log.warning("kraken warning", extra={"method": method, "warning": w})
            if not errors:
                return payload.get("result")
            err = error_for(method, errors, self._clock.time())
            cat = err.category
            if cat == ErrorCategory.UNKNOWN_OUTCOME:
                raise err
            if cat == ErrorCategory.RATE_LIMIT:
                last = err
                await self._sleep(attempt, method, "rate_limit", at_least=err.retry_after_s or 0.0)
                continue
            if cat == ErrorCategory.INVALID_NONCE and nonce_retries == 0:
                nonce_retries += 1  # rejected before processing; a fresh nonce is generated on resend
                last = err
                continue
            if cat == ErrorCategory.RETRYABLE:
                if not idempotent:
                    raise OutcomeUnknownError(method, errors) from None
                last = err
                await self._sleep(attempt, method, "retryable")
                continue
            raise err
        assert last is not None
        raise last

    async def _sleep(self, attempt: int, method: str, why: str, at_least: float = 0.0) -> None:
        delay = max(at_least, self._retry.backoff(attempt))
        log.info("kraken retry",
                 extra={"method": method, "reason": why, "attempt": attempt + 1, "delay_s": round(delay, 3)})
        await self._clock.sleep(delay)

    # ------------------------------------------------------------------ public endpoints
    async def server_time(self) -> dict[str, Any]:
        r: dict[str, Any] = await self._public_call("Time")
        return r

    async def system_status(self) -> str:
        r = await self._public_call("SystemStatus")
        return str(r["status"])

    async def asset_pairs(self, pairs: Sequence[str] | None = None) -> dict[str, Instrument]:
        """Instruments keyed by WS v1 name, with the WS v2 symbol merged from the assetVersion=1 listing."""
        params = {"pair": ",".join(pairs)} if pairs else {}
        legacy = parse_asset_pairs(await self._public_call("AssetPairs", params or None))
        display = await self._public_call("AssetPairs", {**params, "assetVersion": "1"})
        return merge_display_symbols(legacy, display)

    async def ticker(self, pairs: Sequence[str]) -> dict[str, BookTop]:
        return parse_ticker(await self._public_call("Ticker", {"pair": ",".join(pairs)}))

    async def ohlc(self, pair: str, interval_min: int, since: int | None = None) -> dict[str, Any]:
        params = {"pair": pair, "interval": str(interval_min)}
        if since is not None:
            params["since"] = str(since)
        r: dict[str, Any] = await self._public_call("OHLC", params)
        return r

    async def depth(self, pair: str, count: int = 100) -> dict[str, Any]:
        r: dict[str, Any] = await self._public_call("Depth", {"pair": pair, "count": str(count)})
        return r

    # ------------------------------------------------------------------ private reads
    async def balance_ex(self) -> dict[str, dict[str, Decimal]]:
        r = await self._private_call("BalanceEx")
        return {a: {k: Decimal(str(v)) for k, v in d.items() if k in ("balance", "hold_trade", "credit", "credit_used")}
                for a, d in r.items()}

    async def trade_volume(self, pairs: Sequence[str]) -> dict[str, Any]:
        r: dict[str, Any] = await self._private_call("TradeVolume", [("pair", ",".join(pairs))])
        return r

    async def open_orders(self, *, cl_ord_id: str | None = None) -> dict[str, Any]:
        data = [("trades", "true")] + ([("cl_ord_id", cl_ord_id)] if cl_ord_id is not None else [])
        r: dict[str, Any] = await self._private_call("OpenOrders", data)
        return r

    async def closed_orders(self, start: int | None = None, *, cl_ord_id: str | None = None,
                            closetime: str | None = None) -> dict[str, Any]:
        """`start` is exclusive (unix seconds); `cl_ord_id` restricts results server side (docs: Get Closed Orders);
        pages hold at most 50 orders, so callers must check `count` against what they received."""
        data = [("trades", "true")]
        if start is not None:
            data.append(("start", str(start)))
        if cl_ord_id is not None:
            data.append(("cl_ord_id", cl_ord_id))
        if closetime is not None:
            data.append(("closetime", closetime))
        r: dict[str, Any] = await self._private_call("ClosedOrders", data)
        return r

    async def query_orders(self, txids: Sequence[str]) -> dict[str, Any]:
        r: dict[str, Any] = await self._private_call("QueryOrders", [("txid", ",".join(txids)), ("trades", "true")])
        return r

    async def trades_history(self, start: int | None = None) -> dict[str, Any]:
        data = [("start", str(start))] if start is not None else []
        r: dict[str, Any] = await self._private_call("TradesHistory", data)
        return r

    async def query_trades(self, trade_ids: Sequence[str]) -> dict[str, Any]:
        """Fill details (price, vol, cost, fee, ordertxid, time) for trade ids listed in an order's `trades`."""
        r: dict[str, Any] = await self._private_call("QueryTrades", [("txid", ",".join(trade_ids))])
        return r

    async def websockets_token(self) -> str:
        r = await self._private_call("GetWebSocketsToken")
        return str(r["token"])

    # ------------------------------------------------------------------ trading (guarded)
    async def add_order(self, req: AddOrderRequest, instrument: Instrument) -> dict[str, Any]:
        if not self._trading:
            raise TradingDisabledError("AddOrder refused: client built without trading")
        problems = req.problems(instrument)
        if problems:
            raise LocalValidationError("; ".join(problems))
        now = datetime.fromtimestamp(self._clock.time(), UTC)
        r: dict[str, Any] = await self._private_call("AddOrder", req.to_params(now), engine_pair=instrument.altname,
                                                     engine_units=1)
        return r

    async def amend_order(self, *, instrument: Instrument, order_age_s: float | None, txid: str | None = None,
                          cl_ord_id: str | None = None, limit_price: Decimal | None = None,
                          trigger_price: Decimal | None = None, post_only: bool = False) -> dict[str, Any]:
        if not self._trading:
            raise TradingDisabledError("AmendOrder refused: client built without trading")
        if (txid is None) == (cl_ord_id is None):
            raise LocalValidationError("exactly one of txid / cl_ord_id")
        data: list[tuple[str, str]] = [("txid", txid)] if txid else [("cl_ord_id", str(cl_ord_id))]
        for name, v in (("limit_price", limit_price), ("trigger_price", trigger_price)):
            if v is not None:
                if v % instrument.tick_size != 0:
                    raise LocalValidationError(f"{name} {v} not a multiple of tick_size {instrument.tick_size}")
                data.append((name, str(to_db(v))))
        if limit_price is None and trigger_price is None:
            raise LocalValidationError("nothing to amend")
        if post_only:
            data.append(("post_only", "true"))
        r: dict[str, Any] = await self._private_call("AmendOrder", data, engine_pair=instrument.altname,
                                                     engine_units=engine_cost("amend", order_age_s))
        return r

    async def cancel_order(self, *, pair_altname: str, order_age_s: float | None, txid: str | None = None,
                           cl_ord_id: str | None = None) -> dict[str, Any]:
        if (txid is None) == (cl_ord_id is None):
            raise LocalValidationError("exactly one of txid / cl_ord_id")
        data = [("txid", txid)] if txid else [("cl_ord_id", str(cl_ord_id))]
        r: dict[str, Any] = await self._private_call("CancelOrder", data, engine_pair=pair_altname,
                                                     engine_units=engine_cost("cancel", order_age_s))
        return r

    async def cancel_all(self) -> dict[str, Any]:
        r: dict[str, Any] = await self._private_call("CancelAll")
        return r

    async def cancel_all_orders_after(self, timeout_s: int) -> dict[str, Any]:
        """Kraken dead man's switch; 0 disables it. Kraken requires timeout < 86400."""
        if not 0 <= timeout_s < 86400:
            raise LocalValidationError("timeout must be in [0, 86400)")
        r: dict[str, Any] = await self._private_call("CancelAllOrdersAfter", [("timeout", str(timeout_s))])
        return r
