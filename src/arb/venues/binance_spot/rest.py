"""[MD-001] Binance Spot REST Read-Only & Reconciliation Query Adapter."""
from __future__ import annotations

import hashlib
import hmac
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from arb.config.settings import VenueCredentials, mask_sensitive_text
from arb.domain.models import (
    BalanceSnapshot,
    Fill,
    Instrument,
    InstrumentType,
    MarketSnapshot,
    OrderBook,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    PriceLevel,
    PriceType,
    TradingStatus,
)
from arb.observability.telemetry import (
    AlertManager,
    MetricsRegistry,
    StructuredLogger,
    TraceContext,
)
from arb.venues.base import (
    AccountAdapter,
    ErrorCategory,
    ExchangeError,
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    TradingAdapter,
)


class HttpResponsePayload(BaseModel):
    status_code: int
    json_body: Optional[Dict[str, Any] | List[Any]] = None
    text_body: str = ""
    headers: Dict[str, str] = Field(default_factory=dict)
    elapsed_ms: int = 0


class HttpTransport(ABC):
    """Pluggable async HTTP transport interface with explicit timeout and connection pooling semantics."""

    @abstractmethod
    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 3000,
    ) -> HttpResponsePayload:
        pass


class BinanceSpotRestAdapter(
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    AccountAdapter,
    TradingAdapter,
):
    """
    [MD-001] Binance Spot REST Adapter.
    - Strictly read-only in Phase 1/Sprint 2 (`submit_order` and `cancel_order` hard-blocked unless explicitly unlocked in Sprint 5).
    - Classifies HTTP 429 (RATE_LIMITED), HTTP 418 (IP_BANNED), 401/403 (AUTH_PERMISSION), 5xx/Timeout (TRANSIENT_NETWORK / UNKNOWN_ORDER_STATE).
    - Enforces server clock skew guard and scrubs all secrets/signatures from logs and errors.
    """

    VENUE_ID = "binance_spot"

    STATUS_MAP: Dict[str, OrderStatus] = {
        "NEW": OrderStatus.NEW,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "PENDING_CANCEL": OrderStatus.CANCEL_PENDING,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.EXPIRED,
        "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    }

    TRADING_STATUS_MAP: Dict[str, TradingStatus] = {
        "TRADING": TradingStatus.TRADING,
        "HALT": TradingStatus.HALTED,
        "BREAK": TradingStatus.BREAK,
        "PRE_TRADING": TradingStatus.PRE_OPEN,
        "POST_TRADING": TradingStatus.POST_CLOSE,
    }

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = "https://api.binance.com",
        credentials: Optional[VenueCredentials] = None,
        metrics: Optional[MetricsRegistry] = None,
        logger: Optional[StructuredLogger] = None,
        alerts: Optional[AlertManager] = None,
        timeout_ms: int = 3000,
        max_clock_skew_ms: int = 1000,
        read_only_mode: bool = True,
    ) -> None:
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._metrics = metrics or MetricsRegistry()
        self._logger = logger or StructuredLogger(
            known_secrets=credentials.collect_raw_secrets() if credentials else []
        )
        self._alerts = alerts
        self._timeout_ms = timeout_ms
        self._max_clock_skew_ms = max_clock_skew_ms
        self._read_only_mode = read_only_mode
        self._ip_banned = False
        self._rate_limit_backoff_until_ms: int = 0

    @property
    def venue_id(self) -> str:
        return self.VENUE_ID

    @property
    def is_ip_banned(self) -> bool:
        return self._ip_banned

    @property
    def rate_limit_backoff_until_ms(self) -> int:
        return self._rate_limit_backoff_until_ms

    @staticmethod
    def canonical_instrument_id(base_asset: str, quote_asset: str) -> str:
        return f"{base_asset.upper()}-{quote_asset.upper()}"

    @staticmethod
    def to_binance_symbol(instrument_id: str) -> str:
        return instrument_id.replace("-", "").replace("/", "").upper()

    def _sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._credentials or not self._credentials.api_key or not self._credentials.api_secret:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message="Authenticated endpoint called without Binance API credentials",
                http_status=401,
            )
        signed = dict(params)
        signed.setdefault("timestamp", int(time.time() * 1000))
        signed.setdefault("recvWindow", 5000)
        query_string = urlencode(signed)
        secret_bytes = self._credentials.api_secret.get_secret_value().encode("utf-8")
        signature = hmac.new(secret_bytes, query_string.encode("utf-8"), hashlib.sha256).hexdigest()
        signed["signature"] = signature
        return signed

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        is_write_operation: bool = False,
    ) -> Any:
        ctx = TraceContext(venue_id=self.VENUE_ID)
        if self._ip_banned:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.IP_BANNED,
                message="Binance REST adapter is locked due to prior HTTP 418 IP ban",
                http_status=418,
                is_write_operation=is_write_operation,
            )

        req_params = dict(params or {})
        headers: Dict[str, str] = {}
        if signed:
            req_params = self._sign_params(req_params)
            headers["X-MBX-APIKEY"] = self._credentials.api_key.get_secret_value()  # type: ignore[union-attr]

        url = f"{self._base_url}{path}"
        try:
            resp = await self._transport.request(
                method=method,
                url=url,
                params=req_params,
                headers=headers,
                timeout_ms=self._timeout_ms,
            )
        except TimeoutError as exc:
            self._metrics.inc_counter(
                "arb_rest_errors_total",
                labels={"venue_id": self.VENUE_ID, "path": path, "category": "timeout"},
            )
            cat = (
                ErrorCategory.UNKNOWN_ORDER_STATE
                if is_write_operation
                else ErrorCategory.TRANSIENT_NETWORK
            )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=cat,
                message=f"Request timeout ({self._timeout_ms}ms) on {method} {path}",
                is_write_operation=is_write_operation,
            ) from exc
        except ConnectionError as exc:
            self._metrics.inc_counter(
                "arb_rest_errors_total",
                labels={"venue_id": self.VENUE_ID, "path": path, "category": "connection_reset"},
            )
            cat = (
                ErrorCategory.UNKNOWN_ORDER_STATE
                if is_write_operation
                else ErrorCategory.TRANSIENT_NETWORK
            )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=cat,
                message=f"Connection error on {method} {path}: {mask_sensitive_text(str(exc))}",
                is_write_operation=is_write_operation,
            ) from exc

        self._metrics.inc_counter(
            "arb_rest_requests_total",
            labels={"venue_id": self.VENUE_ID, "path": path, "status": str(resp.status_code)},
        )

        # Check rate limit weight headers if present
        used_weight = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
        if used_weight and str(used_weight).isdigit():
            self._metrics.set_gauge(
                "arb_binance_used_weight_1m",
                float(used_weight),
                labels={"venue_id": self.VENUE_ID},
            )

        if resp.status_code == 200:
            if resp.json_body is None:
                raise ExchangeError(
                    venue_id=self.VENUE_ID,
                    category=ErrorCategory.INVALID_RESPONSE,
                    message=f"Empty JSON body on HTTP 200 from {path}",
                    http_status=200,
                    is_write_operation=is_write_operation,
                )
            return resp.json_body

        native_code: Optional[str] = None
        native_msg: str = resp.text_body
        if isinstance(resp.json_body, dict):
            if "code" in resp.json_body:
                native_code = str(resp.json_body["code"])
            if "msg" in resp.json_body:
                native_msg = str(resp.json_body["msg"])
        native_msg = mask_sensitive_text(native_msg)

        # HTTP 418: IP Auto-banned for continuing to send requests after 429
        if resp.status_code == 418:
            self._ip_banned = True
            retry_after_sec = int(resp.headers.get("Retry-After", "120"))
            self._rate_limit_backoff_until_ms = int(time.time() * 1000) + retry_after_sec * 1000
            if self._alerts:
                self._alerts.trigger(
                    "VENUE_IP_BAN_OR_418",
                    f"Binance HTTP 418 IP Ban on {path} (retry_after={retry_after_sec}s)",
                    ctx,
                )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.IP_BANNED,
                message=f"HTTP 418 IP Ban: {native_msg}",
                http_status=418,
                native_code=native_code,
                is_write_operation=is_write_operation,
                retry_after_ms=retry_after_sec * 1000,
            )

        # HTTP 429: Rate limit exceeded -> backoff required
        if resp.status_code == 429 or native_code == "-1003":
            retry_after_sec = int(resp.headers.get("Retry-After", "5"))
            backoff_ms = retry_after_sec * 1000
            self._rate_limit_backoff_until_ms = int(time.time() * 1000) + backoff_ms
            if self._alerts:
                self._alerts.trigger(
                    "VENUE_HTTP_429_RATE_LIMITED",
                    f"Binance HTTP 429 on {path} (backoff={backoff_ms}ms)",
                    ctx,
                )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.RATE_LIMITED,
                message=f"Rate limited on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code,
                is_write_operation=is_write_operation,
                retry_after_ms=backoff_ms,
            )

        # HTTP 401 / 403 or -2014 / -2015: Authentication or permission error
        if resp.status_code in (401, 403) or native_code in ("-2014", "-2015", "-1022"):
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message=f"Auth/permission failure on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code,
                is_write_operation=is_write_operation,
            )

        # HTTP 5xx or -1007 (Timeout waiting for backend): UNKNOWN for writes, TRANSIENT for reads
        if resp.status_code >= 500 or native_code in ("-1001", "-1007"):
            cat = (
                ErrorCategory.UNKNOWN_ORDER_STATE
                if is_write_operation
                else ErrorCategory.TRANSIENT_NETWORK
            )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=cat,
                message=f"Server/unknown state ({resp.status_code}) on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code,
                is_write_operation=is_write_operation,
            )

        # Remaining 4xx errors (e.g. -1013 Filter failure, -2010 New order rejected)
        raise ExchangeError(
            venue_id=self.VENUE_ID,
            category=ErrorCategory.DEFINITE_REJECT,
            message=f"Request rejected ({resp.status_code}) on {path}: {native_msg}",
            http_status=resp.status_code,
            native_code=native_code,
            is_write_operation=is_write_operation,
        )

    async def get_server_time(self, local_now_utc: Optional[datetime] = None) -> datetime:
        """Fetch `/api/v3/time` and validate clock skew against local UTC clock."""
        body = await self._request("GET", "/api/v3/time")
        if not isinstance(body, dict) or "serverTime" not in body:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message="Missing 'serverTime' in /api/v3/time response",
                http_status=200,
            )
        server_ms = int(body["serverTime"])
        server_dt = datetime.fromtimestamp(server_ms / 1000.0, tz=timezone.utc)
        ref_now = local_now_utc or datetime.now(timezone.utc)
        skew_ms = abs(int((ref_now - server_dt).total_seconds() * 1000))
        self._metrics.set_gauge(
            "arb_venue_clock_skew_ms", float(skew_ms), labels={"venue_id": self.VENUE_ID}
        )
        if skew_ms > self._max_clock_skew_ms:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.TRANSIENT_NETWORK,
                message=f"Server clock skew {skew_ms}ms exceeds max_clock_skew_ms={self._max_clock_skew_ms}ms",
                http_status=200,
                native_code="-1021",
            )
        return server_dt

    async def fetch_instruments(self) -> List[Instrument]:
        """Fetch `/api/v3/exchangeInfo` and parse PRICE_FILTER, LOT_SIZE, and NOTIONAL/MIN_NOTIONAL."""
        body = await self._request("GET", "/api/v3/exchangeInfo")
        if not isinstance(body, dict) or "symbols" not in body or not isinstance(body["symbols"], list):
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message="Malformed /api/v3/exchangeInfo payload: missing 'symbols' list",
                http_status=200,
            )

        now_utc = datetime.now(timezone.utc)
        instruments: List[Instrument] = []
        for sym_obj in body["symbols"]:
            try:
                venue_symbol = str(sym_obj["symbol"])
                base_asset = str(sym_obj["baseAsset"])
                quote_asset = str(sym_obj["quoteAsset"])
                raw_status = str(sym_obj.get("status", "HALT"))
                trading_status = self.TRADING_STATUS_MAP.get(raw_status, TradingStatus.HALTED)
                if sym_obj.get("isSpotTradingAllowed") is False:
                    trading_status = TradingStatus.HALTED

                tick_size: Optional[Decimal] = None
                step_size: Optional[Decimal] = None
                min_qty: Decimal = Decimal("0")
                min_notional: Optional[Decimal] = None

                for f in sym_obj.get("filters", []):
                    ftype = f.get("filterType")
                    if ftype == "PRICE_FILTER":
                        tick_size = Decimal(str(f["tickSize"]))
                    elif ftype == "LOT_SIZE":
                        step_size = Decimal(str(f["stepSize"]))
                        min_qty = Decimal(str(f.get("minQty", "0")))
                    elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                        min_notional = Decimal(str(f["minNotional"]))

                if tick_size is None or step_size is None or min_notional is None:
                    raise KeyError("Missing PRICE_FILTER, LOT_SIZE, or MIN_NOTIONAL filter")

                inst_id = self.canonical_instrument_id(base_asset, quote_asset)
                instruments.append(
                    Instrument(
                        venue_id=self.VENUE_ID,
                        instrument_id=inst_id,
                        venue_symbol=venue_symbol,
                        instrument_type=InstrumentType.SPOT,
                        base_asset=base_asset,
                        quote_asset=quote_asset,
                        settlement_asset=quote_asset,
                        contract_multiplier=Decimal("1"),
                        tick_size=tick_size,
                        step_size=step_size,
                        min_notional=min_notional,
                        min_qty=min_qty,
                        trading_status=trading_status,
                        event_time_utc=now_utc,
                        receive_time_utc=now_utc,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise ExchangeError(
                    venue_id=self.VENUE_ID,
                    category=ErrorCategory.INVALID_RESPONSE,
                    message=f"Invalid symbol schema in exchangeInfo: {exc}",
                    http_status=200,
                ) from exc

        return instruments

    async def fetch_order_book(self, instrument_id: str, depth: int = 20) -> OrderBook:
        """Fetch `/api/v3/depth` L2 snapshot with `lastUpdateId`."""
        binance_sym = self.to_binance_symbol(instrument_id)
        body = await self._request("GET", "/api/v3/depth", params={"symbol": binance_sym, "limit": depth})
        if not isinstance(body, dict) or "lastUpdateId" not in body or "bids" not in body or "asks" not in body:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message=f"Malformed /api/v3/depth response for {instrument_id}",
                http_status=200,
            )

        now_utc = datetime.now(timezone.utc)
        bids = [
            PriceLevel(price=Decimal(str(level[0])), quantity=Decimal(str(level[1])))
            for level in body["bids"]
            if Decimal(str(level[1])) > Decimal("0")
        ]
        asks = [
            PriceLevel(price=Decimal(str(level[0])), quantity=Decimal(str(level[1])))
            for level in body["asks"]
            if Decimal(str(level[1])) > Decimal("0")
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            last_update_id=int(body["lastUpdateId"]),
            is_valid=True,
            event_time_utc=now_utc,
            receive_time_utc=now_utc,
        )

    async def fetch_market_snapshot(self, instrument_id: str) -> MarketSnapshot:
        book = await self.fetch_order_book(instrument_id=instrument_id, depth=20)
        parts = instrument_id.split("-")
        quote_asset = parts[1] if len(parts) > 1 else "USDT"
        return MarketSnapshot(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            quote_asset=quote_asset,
            bid_levels=book.bids,
            ask_levels=book.asks,
            sequence_id=book.last_update_id,
            event_time_utc=book.event_time_utc,
            receive_time_utc=book.receive_time_utc,
        )

    async def fetch_balances(self) -> List[BalanceSnapshot]:
        """Fetch `/api/v3/account` balances (requires authorized credentials)."""
        body = await self._request("GET", "/api/v3/account", signed=True)
        if not isinstance(body, dict) or "balances" not in body:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message="Malformed /api/v3/account response: missing 'balances'",
                http_status=200,
            )

        now_utc = datetime.now(timezone.utc)
        snapshots: List[BalanceSnapshot] = []
        for b in body["balances"]:
            asset = str(b["asset"])
            free_val = Decimal(str(b["free"]))
            locked_val = Decimal(str(b["locked"]))
            total_val = free_val + locked_val
            if total_val > Decimal("0"):
                snapshots.append(
                    BalanceSnapshot(
                        venue_id=self.VENUE_ID,
                        account_id="binance_spot_main",
                        asset=asset,
                        total_balance=total_val,
                        free_balance=free_val,
                        locked_balance=locked_val,
                        event_time_utc=now_utc,
                        receive_time_utc=now_utc,
                    )
                )
        return snapshots

    async def fetch_positions(self) -> List[PositionSnapshot]:
        """Spot adapter has no derivative positions (returns empty list)."""
        return []

    async def query_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        """Query `/api/v3/order` for reconciliation."""
        params: Dict[str, Any] = {
            "symbol": self.to_binance_symbol(instrument_id),
            "origClientOrderId": client_order_id,
        }
        if venue_order_id:
            params["orderId"] = venue_order_id
        body = await self._request("GET", "/api/v3/order", params=params, signed=True)
        if not isinstance(body, dict) or "status" not in body:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message="Malformed /api/v3/order response",
                http_status=200,
            )

        native_status = str(body["status"])
        mapped_status = self.STATUS_MAP.get(native_status, OrderStatus.UNKNOWN)
        orig_qty = Decimal(str(body.get("origQty", "0")))
        exec_qty = Decimal(str(body.get("executedQty", "0")))
        cumm_quote = Decimal(str(body.get("cummulativeQuoteQty", "0")))
        avg_price = (cumm_quote / exec_qty) if exec_qty > Decimal("0") else None
        limit_px = Decimal(str(body.get("price", "0")))
        side_str = str(body.get("side", "BUY")).upper()

        return OrderEvent(
            cycle_id="reconcile",
            strategy_id="reconcile",
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            client_order_id=str(body.get("clientOrderId", client_order_id)),
            venue_order_id=str(body.get("orderId", "")),
            side=OrderSide.BUY if side_str == "BUY" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            status=mapped_status,
            native_venue_status=native_status,
            order_quantity=orig_qty,
            limit_price=limit_px if limit_px > Decimal("0") else None,
            cumulative_filled_qty=exec_qty,
            average_fill_price=avg_price,
        )

    async def fetch_fills(
        self,
        instrument_id: str,
        client_order_id: Optional[str] = None,
    ) -> List[Fill]:
        """Query `/api/v3/myTrades` for fill reconciliation."""
        params: Dict[str, Any] = {"symbol": self.to_binance_symbol(instrument_id)}
        body = await self._request("GET", "/api/v3/myTrades", params=params, signed=True)
        if not isinstance(body, list):
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message="Malformed /api/v3/myTrades response: expected list",
                http_status=200,
            )

        fills: List[Fill] = []
        for item in body:
            trade_id = str(item["id"])
            ord_id = str(item["orderId"])
            t_ms = int(item.get("time", int(time.time() * 1000)))
            ev_dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
            fills.append(
                Fill(
                    fill_id=f"bspot_{trade_id}",
                    venue_trade_id=trade_id,
                    cycle_id="reconcile",
                    strategy_id="reconcile",
                    venue_id=self.VENUE_ID,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id or ord_id,
                    venue_order_id=ord_id,
                    side=OrderSide.BUY if item.get("isBuyer", True) else OrderSide.SELL,
                    fill_qty=Decimal(str(item["qty"])),
                    fill_price=Decimal(str(item["price"])),
                    price_type=PriceType.ACTUAL_FILL_PRICE,
                    fee_amount=Decimal(str(item["commission"])),
                    fee_asset=str(item["commissionAsset"]),
                    is_maker=bool(item.get("isMaker", False)),
                    event_time_utc=ev_dt,
                )
            )
        return fills

    async def submit_order(
        self,
        cycle_id: str,
        strategy_id: str,
        instrument_id: str,
        client_order_id: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        limit_price: Optional[Decimal] = None,
    ) -> OrderEvent:
        """
        [MD-001] Phase 1 is strictly read-only by default.
        When unlocked in Sprint 5, any timeout/5xx returns OrderStatus.UNKNOWN (never definite failure).
        """
        if self._read_only_mode:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message="BinanceSpotRestAdapter is in strictly READ-ONLY mode (Sprint 2 MD-001 gate)",
                http_status=403,
                is_write_operation=True,
            )

        try:
            params: Dict[str, Any] = {
                "symbol": self.to_binance_symbol(instrument_id),
                "side": side.value.upper(),
                "type": order_type.value.upper(),
                "quantity": str(quantity),
                "newClientOrderId": client_order_id,
            }
            if limit_price is not None:
                params["price"] = str(limit_price)
            body = await self._request(
                "POST", "/api/v3/order", params=params, signed=True, is_write_operation=True
            )
            native_status = str(body.get("status", "NEW"))
            return OrderEvent(
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                venue_id=self.VENUE_ID,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=str(body.get("orderId", "")),
                side=side,
                order_type=order_type,
                status=self.STATUS_MAP.get(native_status, OrderStatus.UNKNOWN),
                native_venue_status=native_status,
                order_quantity=quantity,
                limit_price=limit_price,
                cumulative_filled_qty=Decimal(str(body.get("executedQty", "0"))),
            )
        except ExchangeError as exc:
            if exc.requires_reconciliation:
                # Timeout or 5xx on order placement MUST NEVER be treated as definite failure!
                return OrderEvent(
                    cycle_id=cycle_id,
                    strategy_id=strategy_id,
                    venue_id=self.VENUE_ID,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    side=side,
                    order_type=order_type,
                    status=OrderStatus.UNKNOWN,
                    native_venue_status=f"UNKNOWN_ON_{exc.category.value.upper()}",
                    order_quantity=quantity,
                    limit_price=limit_price,
                    error_code=exc.native_code,
                    error_message=exc.message,
                )
            raise

    async def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        if self._read_only_mode:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message="BinanceSpotRestAdapter is in strictly READ-ONLY mode (Sprint 2 MD-001 gate)",
                http_status=403,
                is_write_operation=True,
            )
        raise NotImplementedError("Unlocked in EXEC-001")
