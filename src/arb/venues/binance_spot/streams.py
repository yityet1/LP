"""[MD-002] Binance Spot WebSocket Stream Collector, Connection Lifecycle & Queue Guard."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from arb.domain.models import PriceLevel, ensure_utc
from arb.marketdata.orderbook import OrderBookBuilder, SequenceStatus
from arb.observability.telemetry import (
    AlertManager,
    MetricsRegistry,
    StructuredLogger,
    TraceContext,
)


class StreamOverloadError(RuntimeError):
    """Raised when bounded WebSocket message queue reaches capacity (never silently drop messages)."""


class BinanceBookTickerEvent(BaseModel):
    venue_id: str = "binance_spot"
    instrument_id: str
    venue_symbol: str
    update_id: int
    best_bid_price: Decimal
    best_bid_qty: Decimal
    best_ask_price: Decimal
    best_ask_qty: Decimal
    event_time_utc: datetime
    receive_time_utc: datetime
    monotonic_receive_ns: int


class BinanceDiffDepthEvent(BaseModel):
    venue_id: str = "binance_spot"
    instrument_id: str
    venue_symbol: str
    first_update_id: int
    final_update_id: int
    bid_deltas: List[PriceLevel]
    ask_deltas: List[PriceLevel]
    event_time_utc: datetime
    receive_time_utc: datetime
    monotonic_receive_ns: int


class BinanceSpotStreamManager:
    """
    [MD-002] Manages Binance Spot WebSocket subscriptions (`bookTicker` & `depthUpdate`),
    connection lifecycle, proactive rotation before 24h limit, disconnect invalidation,
    and bounded queue backpressure circuit breaking.
    """

    VENUE_ID = "binance_spot"

    def __init__(
        self,
        symbol_to_instrument_id: Dict[str, str],
        order_books: Dict[str, OrderBookBuilder],
        max_queue_size: int = 1000,
        rotation_interval_seconds: int = 23 * 3600,  # Rotate proactively at 23h before Binance 24h limit
        heartbeat_timeout_seconds: int = 15,
        metrics: Optional[MetricsRegistry] = None,
        logger: Optional[StructuredLogger] = None,
        alerts: Optional[AlertManager] = None,
    ) -> None:
        self._symbol_map = {k.upper(): v for k, v in symbol_to_instrument_id.items()}
        self._order_books = order_books
        self._max_queue_size = max_queue_size
        self._rotation_interval_seconds = rotation_interval_seconds
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._metrics = metrics or MetricsRegistry()
        self._logger = logger or StructuredLogger()
        self._alerts = alerts

        self._connected: bool = False
        self._connected_since_utc: Optional[datetime] = None
        self._last_message_utc: Optional[datetime] = None
        self._queue: List[BinanceBookTickerEvent | BinanceDiffDepthEvent] = []
        self._latest_bbo: Dict[str, BinanceBookTickerEvent] = {}
        self._circuit_broken: bool = False

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._circuit_broken

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def is_circuit_broken(self) -> bool:
        return self._circuit_broken

    def build_subscription_payload(self, request_id: int = 1, depth_interval_ms: int = 100) -> Dict[str, Any]:
        params: List[str] = []
        for sym in sorted(self._symbol_map.keys()):
            lower = sym.lower()
            params.append(f"{lower}@bookTicker")
            params.append(f"{lower}@depth@{depth_interval_ms}ms")
        return {"method": "SUBSCRIBE", "params": params, "id": request_id}

    def on_connect(self, now_utc: Optional[datetime] = None) -> None:
        """
        Called when WebSocket connection is established or re-established.
        IMPORTANT: Reconnect NEVER reuses stale order book state! All managed books are set to BUFFERING
        until a fresh REST snapshot is fetched and aligned.
        """
        ts = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        self._connected = True
        self._connected_since_utc = ts
        self._last_message_utc = ts
        for inst_id, builder in self._order_books.items():
            builder.invalidate("WS_RECONNECTED_AWAITING_FRESH_SNAPSHOT", SequenceStatus.BUFFERING)
        self._metrics.set_gauge("arb_ws_connected", 1.0, labels={"venue_id": self.VENUE_ID})

    def on_disconnect(self, reason: str = "PEER_CLOSED") -> None:
        """
        Immediately mark all affected order books INVALID on WebSocket disconnect and fire P1 alert.
        """
        self._connected = False
        for inst_id, builder in self._order_books.items():
            builder.invalidate(f"WS_DISCONNECTED:{reason}", SequenceStatus.DISCONNECTED)
        self._metrics.set_gauge("arb_ws_connected", 0.0, labels={"venue_id": self.VENUE_ID})
        if self._alerts:
            self._alerts.trigger(
                "MARKET_DATA_STREAM_DISCONNECTED",
                f"Binance Spot WebSocket disconnected: {reason}",
                TraceContext(venue_id=self.VENUE_ID),
            )

    def needs_proactive_rotation(self, now_utc: Optional[datetime] = None) -> bool:
        """Binance disconnects WebSocket connections at 24h; proactively rotate after 23h."""
        if not self._connected or not self._connected_since_utc:
            return False
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        elapsed = (ref_now - self._connected_since_utc).total_seconds()
        return elapsed >= self._rotation_interval_seconds

    def check_heartbeat(self, now_utc: Optional[datetime] = None) -> bool:
        """Verify stream liveness; if silent beyond heartbeat_timeout_seconds, trigger disconnect invalidation."""
        if not self._connected or not self._last_message_utc:
            return False
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        silence_sec = (ref_now - self._last_message_utc).total_seconds()
        if silence_sec > self._heartbeat_timeout_seconds:
            self.on_disconnect(reason=f"HEARTBEAT_TIMEOUT_{silence_sec:.1f}S")
            return False
        return True

    def on_raw_message(
        self,
        raw_payload: str | Dict[str, Any],
        receive_time_utc: Optional[datetime] = None,
        monotonic_ns: Optional[int] = None,
    ) -> Optional[BinanceBookTickerEvent | BinanceDiffDepthEvent]:
        """
        Parse incoming WebSocket message, enforce bounded queue guard (never silently drop!),
        and feed diff depth updates into the corresponding OrderBookBuilder.
        """
        if self._circuit_broken:
            raise StreamOverloadError("Stream manager is circuit-broken due to prior queue overload")

        if len(self._queue) >= self._max_queue_size:
            self._circuit_broken = True
            self.on_disconnect(reason="QUEUE_OVERLOAD_CIRCUIT_BREAKER")
            raise StreamOverloadError(
                f"WebSocket queue reached max_queue_size={self._max_queue_size}; halting stream (zero silent drop policy)"
            )

        rx_utc = ensure_utc(receive_time_utc) if receive_time_utc else datetime.now(timezone.utc)
        mono_ns = monotonic_ns if monotonic_ns is not None else time.monotonic_ns()
        self._last_message_utc = rx_utc

        try:
            data = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except Exception as exc:
            self._metrics.inc_counter(
                "arb_ws_parse_errors_total", labels={"venue_id": self.VENUE_ID}
            )
            raise ValueError(f"Malformed WebSocket JSON: {exc}") from exc

        # Combined stream wrapper support: {"stream": "...", "data": {...}}
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        if not isinstance(data, dict):
            raise ValueError("WebSocket message payload must be a JSON object")

        # Ignore subscription acks: {"result": null, "id": 1}
        if "result" in data and "id" in data:
            return None

        sym = str(data.get("s", "")).upper()
        if not sym or sym not in self._symbol_map:
            raise ValueError(f"Unrecognized or missing symbol '{sym}' in WebSocket payload")
        inst_id = self._symbol_map[sym]

        event_type = data.get("e")
        # 1. Diff Depth Stream (`e == "depthUpdate"`)
        if event_type == "depthUpdate":
            ev_ms = int(data["E"])
            ev_utc = datetime.fromtimestamp(ev_ms / 1000.0, tz=timezone.utc)
            first_u = int(data["U"])
            final_u = int(data["u"])
            b_deltas = [
                PriceLevel(price=Decimal(str(x[0])), quantity=Decimal(str(x[1])))
                for x in data.get("b", [])
            ]
            a_deltas = [
                PriceLevel(price=Decimal(str(x[0])), quantity=Decimal(str(x[1])))
                for x in data.get("a", [])
            ]
            ev = BinanceDiffDepthEvent(
                instrument_id=inst_id,
                venue_symbol=sym,
                first_update_id=first_u,
                final_update_id=final_u,
                bid_deltas=b_deltas,
                ask_deltas=a_deltas,
                event_time_utc=ev_utc,
                receive_time_utc=rx_utc,
                monotonic_receive_ns=mono_ns,
            )
            self._queue.append(ev)
            self._metrics.set_gauge(
                "arb_ws_queue_size", float(len(self._queue)), labels={"venue_id": self.VENUE_ID}
            )
            builder = self._order_books.get(inst_id)
            if builder:
                builder.buffer_or_apply_diff(
                    first_update_id=first_u,
                    final_update_id=final_u,
                    bid_deltas=b_deltas,
                    ask_deltas=a_deltas,
                    event_time_utc=ev_utc,
                    receive_time_utc=rx_utc,
                )
            return ev

        # 2. BookTicker Stream (has "u", "b", "B", "a", "A")
        if "u" in data and "b" in data and "a" in data and "B" in data and "A" in data:
            ev_ms = int(data.get("E", int(rx_utc.timestamp() * 1000)))
            ev_utc = datetime.fromtimestamp(ev_ms / 1000.0, tz=timezone.utc)
            bbo = BinanceBookTickerEvent(
                instrument_id=inst_id,
                venue_symbol=sym,
                update_id=int(data["u"]),
                best_bid_price=Decimal(str(data["b"])),
                best_bid_qty=Decimal(str(data["B"])),
                best_ask_price=Decimal(str(data["a"])),
                best_ask_qty=Decimal(str(data["A"])),
                event_time_utc=ev_utc,
                receive_time_utc=rx_utc,
                monotonic_receive_ns=mono_ns,
            )
            self._queue.append(bbo)
            self._latest_bbo[inst_id] = bbo
            self._metrics.set_gauge(
                "arb_ws_queue_size", float(len(self._queue)), labels={"venue_id": self.VENUE_ID}
            )
            return bbo

        raise ValueError(f"Unsupported Binance WebSocket event structure for {sym}")

    def pop_next_event(self) -> Optional[BinanceBookTickerEvent | BinanceDiffDepthEvent]:
        if not self._queue:
            return None
        ev = self._queue.pop(0)
        self._metrics.set_gauge(
            "arb_ws_queue_size", float(len(self._queue)), labels={"venue_id": self.VENUE_ID}
        )
        return ev
