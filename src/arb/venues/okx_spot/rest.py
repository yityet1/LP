"""[VENUE-001] Second CEX (OKX V5 Spot) Read-Only Market Data, Metadata & Account Adapter."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

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
from arb.venues.binance_spot.rest import HttpTransport


class OkxSpotRestAdapter(
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    AccountAdapter,
    TradingAdapter,
):
    """
    [VENUE-001] OKX V5 Spot REST Adapter (`venue_id = "okx_spot"`).
    - Selected by `META-001` as the second CEX venue.
    - Preserves OKX V5 native error codes (`code`: `"0"` = success, `"50011"` = Rate limit,
      `"50111"`/`"50113"` = Auth failure, `"50001"`/`"50004"` = Service unavailable/timeout,
      `"51000"`/`"51008"`/`"51020"` = Order/parameter definite reject) and native states
      (`live`, `suspend`, `preopen`, `canceled`, `filled`, `partially_filled`).
    - Enforces read-only mode in Sprint 4 (`submit_order` / `cancel_order` hard-blocked by default).
    """

    VENUE_ID = "okx_spot"

    STATE_TO_TRADING_STATUS: Dict[str, TradingStatus] = {
        "live": TradingStatus.TRADING,
        "suspend": TradingStatus.HALTED,
        "preopen": TradingStatus.PRE_OPEN,
        "test": TradingStatus.RESEARCH_ONLY,
    }

    ORDER_STATE_MAP: Dict[str, OrderStatus] = {
        "live": OrderStatus.NEW,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "mmp_canceled": OrderStatus.CANCELED,
    }

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = "https://www.okx.com",
        credentials: Optional[VenueCredentials] = None,
        metrics: Optional[MetricsRegistry] = None,
        logger: Optional[StructuredLogger] = None,
        alerts: Optional[AlertManager] = None,
        timeout_ms: int = 3000,
        max_clock_skew_ms: int = 1000,
        default_min_notional_quote: Decimal = Decimal("10.0"),
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
        self._default_min_notional_quote = default_min_notional_quote
        self._read_only_mode = read_only_mode
        self._rate_limit_backoff_until_ms: int = 0

    @property
    def venue_id(self) -> str:
        return self.VENUE_ID

    @property
    def rate_limit_backoff_until_ms(self) -> int:
        return self._rate_limit_backoff_until_ms

    @staticmethod
    def canonical_instrument_id(base_asset: str, quote_asset: str) -> str:
        return f"{base_asset.upper()}-{quote_asset.upper()}"

    @staticmethod
    def to_okx_inst_id(instrument_id: str) -> str:
        return instrument_id.replace("/", "-").upper()

    def _build_auth_headers(self, method: str, request_path: str) -> Dict[str, str]:
        if (
            not self._credentials
            or not self._credentials.api_key
            or not self._credentials.api_secret
            or not self._credentials.passphrase
        ):
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message="OKX V5 authenticated endpoint requires api_key, api_secret, and passphrase",
                http_status=401,
            )
        ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        prehash = f"{ts_iso}{method.upper()}{request_path}"
        secret_bytes = self._credentials.api_secret.get_secret_value().encode("utf-8")
        digest = hmac.new(secret_bytes, prehash.encode("utf-8"), hashlib.sha256).digest()
        sign_b64 = base64.b64encode(digest).decode("utf-8")
        return {
            "OK-ACCESS-KEY": self._credentials.api_key.get_secret_value(),
            "OK-ACCESS-SIGN": sign_b64,
            "OK-ACCESS-TIMESTAMP": ts_iso,
            "OK-ACCESS-PASSPHRASE": self._credentials.passphrase.get_secret_value(),
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        is_write_operation: bool = False,
    ) -> List[Dict[str, Any]]:
        ctx = TraceContext(venue_id=self.VENUE_ID)
        headers: Dict[str, str] = {}
        if signed:
            headers.update(self._build_auth_headers(method, path))

        url = f"{self._base_url}{path}"
        try:
            resp = await self._transport.request(
                method=method,
                url=url,
                params=params,
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
                message=f"OKX request timeout ({self._timeout_ms}ms) on {method} {path}",
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
                message=f"OKX connection error on {method} {path}: {mask_sensitive_text(str(exc))}",
                is_write_operation=is_write_operation,
            ) from exc

        self._metrics.inc_counter(
            "arb_rest_requests_total",
            labels={"venue_id": self.VENUE_ID, "path": path, "status": str(resp.status_code)},
        )

        body = resp.json_body if isinstance(resp.json_body, dict) else {}
        native_code = str(body.get("code", "")) if body else ""
        native_msg = mask_sensitive_text(str(body.get("msg", resp.text_body)))

        # 1. Rate Limit: HTTP 429 or OKX code 50011
        if resp.status_code == 429 or native_code == "50011":
            backoff_ms = int(resp.headers.get("Retry-After", "2")) * 1000
            self._rate_limit_backoff_until_ms = int(time.time() * 1000) + backoff_ms
            if self._alerts:
                self._alerts.trigger(
                    "VENUE_HTTP_429_RATE_LIMITED",
                    f"OKX rate limit (code={native_code or 429}) on {path}",
                    ctx,
                )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.RATE_LIMITED,
                message=f"OKX rate limited on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code or "50011",
                is_write_operation=is_write_operation,
                retry_after_ms=backoff_ms,
            )

        # 2. Auth / Permission: HTTP 401/403 or OKX codes 50101..50119
        if resp.status_code in (401, 403) or native_code.startswith("501"):
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message=f"OKX auth/permission error on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code,
                is_write_operation=is_write_operation,
            )

        # 3. Server / Timeout / Unknown state: HTTP 5xx or OKX codes 50001, 50004, 50026
        if resp.status_code >= 500 or native_code in ("50001", "50004", "50026"):
            cat = (
                ErrorCategory.UNKNOWN_ORDER_STATE
                if is_write_operation
                else ErrorCategory.TRANSIENT_NETWORK
            )
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=cat,
                message=f"OKX server/timeout error on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code,
                is_write_operation=is_write_operation,
            )

        if resp.status_code != 200 or native_code != "0":
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.DEFINITE_REJECT,
                message=f"OKX rejected request on {path}: {native_msg}",
                http_status=resp.status_code,
                native_code=native_code or str(resp.status_code),
                is_write_operation=is_write_operation,
            )

        data = body.get("data")
        if not isinstance(data, list):
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message=f"OKX response missing 'data' list on {path}",
                http_status=200,
                native_code="0",
            )
        return data

    async def get_server_time(self, local_now_utc: Optional[datetime] = None) -> datetime:
        """Fetch `/api/v5/public/time` and verify clock skew."""
        data = await self._request("GET", "/api/v5/public/time")
        if not data or "ts" not in data[0]:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message="Missing 'ts' in OKX /api/v5/public/time",
                http_status=200,
            )
        server_ms = int(data[0]["ts"])
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
                message=f"OKX server clock skew {skew_ms}ms exceeds {self._max_clock_skew_ms}ms",
                http_status=200,
                native_code="50112",
            )
        return server_dt

    async def fetch_instruments(self) -> List[Instrument]:
        """Fetch `/api/v5/public/instruments?instType=SPOT` and normalize rules."""
        data = await self._request("GET", "/api/v5/public/instruments", params={"instType": "SPOT"})
        now_utc = datetime.now(timezone.utc)
        instruments: List[Instrument] = []
        for item in data:
            try:
                inst_id_native = str(item["instId"])
                base_ccy = str(item["baseCcy"]).upper()
                quote_ccy = str(item["quoteCcy"]).upper()
                tick_sz = Decimal(str(item["tickSz"]))
                lot_sz = Decimal(str(item["lotSz"]))
                min_sz = Decimal(str(item["minSz"]))
                state_raw = str(item.get("state", "suspend")).lower()
                trading_status = self.STATE_TO_TRADING_STATUS.get(state_raw, TradingStatus.HALTED)

                min_notional_raw = item.get("minNotional")
                min_notional = (
                    Decimal(str(min_notional_raw))
                    if min_notional_raw is not None
                    else self._default_min_notional_quote
                )

                canonical_id = self.canonical_instrument_id(base_ccy, quote_ccy)
                instruments.append(
                    Instrument(
                        venue_id=self.VENUE_ID,
                        instrument_id=canonical_id,
                        venue_symbol=inst_id_native,
                        instrument_type=InstrumentType.SPOT,
                        base_asset=base_ccy,
                        quote_asset=quote_ccy,
                        settlement_asset=quote_ccy,
                        contract_multiplier=Decimal("1"),
                        tick_size=tick_sz,
                        step_size=lot_sz,
                        min_notional=min_notional,
                        min_qty=min_sz,
                        trading_status=trading_status,
                        event_time_utc=now_utc,
                        receive_time_utc=now_utc,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise ExchangeError(
                    venue_id=self.VENUE_ID,
                    category=ErrorCategory.INVALID_RESPONSE,
                    message=f"Malformed OKX instrument schema: {exc}",
                    http_status=200,
                ) from exc
        return instruments

    async def fetch_order_book(self, instrument_id: str, depth: int = 20) -> OrderBook:
        """Fetch `/api/v5/market/books` L2 snapshot (4-tuple levels `[px, sz, deprecated, numOrders]`)."""
        okx_inst_id = self.to_okx_inst_id(instrument_id)
        data = await self._request(
            "GET", "/api/v5/market/books", params={"instId": okx_inst_id, "sz": str(depth)}
        )
        if not data or "bids" not in data[0] or "asks" not in data[0]:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.INVALID_RESPONSE,
                message=f"Malformed OKX /api/v5/market/books response for {instrument_id}",
                http_status=200,
            )

        book_obj = data[0]
        rx_utc = datetime.now(timezone.utc)
        ts_ms = int(book_obj.get("ts", int(rx_utc.timestamp() * 1000)))
        ev_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        seq_id = int(book_obj.get("seqId", ts_ms))

        bids = [
            PriceLevel(price=Decimal(str(lvl[0])), quantity=Decimal(str(lvl[1])))
            for lvl in book_obj["bids"]
            if Decimal(str(lvl[1])) > Decimal("0")
        ]
        asks = [
            PriceLevel(price=Decimal(str(lvl[0])), quantity=Decimal(str(lvl[1])))
            for lvl in book_obj["asks"]
            if Decimal(str(lvl[1])) > Decimal("0")
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderBook(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            last_update_id=seq_id,
            is_valid=True,
            event_time_utc=ev_utc,
            receive_time_utc=rx_utc,
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
        """Fetch `/api/v5/account/balance` and parse `details[]` (`ccy`, `eq`, `availBal`, `frozenBal`)."""
        data = await self._request("GET", "/api/v5/account/balance", signed=True)
        now_utc = datetime.now(timezone.utc)
        snapshots: List[BalanceSnapshot] = []
        for acct in data:
            for det in acct.get("details", []):
                ccy = str(det["ccy"]).upper()
                avail = Decimal(str(det.get("availBal", det.get("cashBal", "0"))))
                frozen = Decimal(str(det.get("frozenBal", "0")))
                total = Decimal(str(det.get("eq", avail + frozen)))
                if total > Decimal("0") or avail > Decimal("0"):
                    snapshots.append(
                        BalanceSnapshot(
                            venue_id=self.VENUE_ID,
                            account_id="okx_spot_main",
                            asset=ccy,
                            total_balance=total,
                            free_balance=avail,
                            locked_balance=frozen,
                            event_time_utc=now_utc,
                            receive_time_utc=now_utc,
                        )
                    )
        return snapshots

    async def fetch_positions(self) -> List[PositionSnapshot]:
        return []

    async def query_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        params: Dict[str, Any] = {
            "instId": self.to_okx_inst_id(instrument_id),
            "clOrdId": client_order_id,
        }
        if venue_order_id:
            params["ordId"] = venue_order_id
        data = await self._request("GET", "/api/v5/trade/order", params=params, signed=True)
        if not data:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.DEFINITE_REJECT,
                message=f"OKX order {client_order_id} not found",
                http_status=200,
                native_code="51603",
            )
        item = data[0]
        native_state = str(item.get("state", "live")).lower()
        avg_px_str = str(item.get("avgPx", "")).strip()
        px_str = str(item.get("px", "")).strip()
        return OrderEvent(
            cycle_id="reconcile",
            strategy_id="reconcile",
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            client_order_id=str(item.get("clOrdId", client_order_id)),
            venue_order_id=str(item.get("ordId", "")),
            side=OrderSide.BUY if str(item.get("side", "buy")).lower() == "buy" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            status=self.ORDER_STATE_MAP.get(native_state, OrderStatus.UNKNOWN),
            native_venue_status=native_state,
            order_quantity=Decimal(str(item.get("sz", "0"))),
            limit_price=Decimal(px_str) if px_str and Decimal(px_str) > Decimal("0") else None,
            cumulative_filled_qty=Decimal(str(item.get("accFillSz", "0"))),
            average_fill_price=Decimal(avg_px_str) if avg_px_str and Decimal(avg_px_str) > Decimal("0") else None,
        )

    async def fetch_fills(
        self,
        instrument_id: str,
        client_order_id: Optional[str] = None,
    ) -> List[Fill]:
        params: Dict[str, Any] = {
            "instType": "SPOT",
            "instId": self.to_okx_inst_id(instrument_id),
        }
        data = await self._request("GET", "/api/v5/trade/fills", params=params, signed=True)
        fills: List[Fill] = []
        for item in data:
            trade_id = str(item["tradeId"])
            ord_id = str(item["ordId"])
            cl_id = str(item.get("clOrdId") or client_order_id or ord_id)
            ts_ms = int(item.get("ts", int(time.time() * 1000)))
            ev_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            # Note: OKX V5 reports fee as negative string (e.g. "-0.001"), normalize to positive fee_amount
            raw_fee = abs(Decimal(str(item.get("fee", "0"))))
            fills.append(
                Fill(
                    fill_id=f"okxspot_{trade_id}",
                    venue_trade_id=trade_id,
                    cycle_id="reconcile",
                    strategy_id="reconcile",
                    venue_id=self.VENUE_ID,
                    instrument_id=instrument_id,
                    client_order_id=cl_id,
                    venue_order_id=ord_id,
                    side=OrderSide.BUY if str(item.get("side", "buy")).lower() == "buy" else OrderSide.SELL,
                    fill_qty=Decimal(str(item["fillSz"])),
                    fill_price=Decimal(str(item["fillPx"])),
                    price_type=PriceType.ACTUAL_FILL_PRICE,
                    fee_amount=raw_fee,
                    fee_asset=str(item.get("feeCcy", "USDT")).upper(),
                    is_maker=(str(item.get("execType", "T")).upper() == "M"),
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
        if self._read_only_mode:
            raise ExchangeError(
                venue_id=self.VENUE_ID,
                category=ErrorCategory.AUTH_PERMISSION,
                message="OkxSpotRestAdapter is in strictly READ-ONLY mode (Sprint 4 VENUE-001 gate)",
                http_status=403,
                is_write_operation=True,
            )
        raise NotImplementedError("Unlocked in EXEC-001")

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
                message="OkxSpotRestAdapter is in strictly READ-ONLY mode (Sprint 4 VENUE-001 gate)",
                http_status=403,
                is_write_operation=True,
            )
        raise NotImplementedError("Unlocked in EXEC-001")
