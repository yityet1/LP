"""[VENUE-001] OKX V5 Spot WebSocket OrderBook Collector & Sequence Validator."""
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
from arb.venues.binance_spot.streams import StreamOverloadError


class OkxOrderBookEvent(BaseModel):
    venue_id: str = "okx_spot"
    instrument_id: str
    venue_symbol: str
    action: str  # "snapshot" | "update"
    prev_seq_id: int
    seq_id: int
    bids: List[PriceLevel]
    asks: List[PriceLevel]
    event_time_utc: datetime
    receive_time_utc: datetime
    monotonic_receive_ns: int


class OkxSpotStreamManager:
    """
    [VENUE-001] Manages OKX V5 WebSocket `books` / `bbo-tbt` channels:
    - Handles OKX's native `action: "snapshot"` (initial full book with `seqId`) and
      `action: "update"` (incremental delta with `prevSeqId` and `seqId`).
    - Strictly enforces `prevSeqId == builder.last_update_id` on updates; any mismatch
      immediately invalidates the order book (`SequenceStatus.GAP_DETECTED`).
    - Invalidates all managed order books on disconnect (`SequenceStatus.DISCONNECTED`).
    """

    VENUE_ID = "okx_spot"

    def __init__(
        self,
        symbol_to_instrument_id: Dict[str, str],
        order_books: Dict[str, OrderBookBuilder],
        max_queue_size: int = 1000,
        metrics: Optional[MetricsRegistry] = None,
        logger: Optional[StructuredLogger] = None,
        alerts: Optional[AlertManager] = None,
    ) -> None:
        self._symbol_map = {k.upper(): v for k, v in symbol_to_instrument_id.items()}
        self._order_books = order_books
        self._max_queue_size = max_queue_size
        self._metrics = metrics or MetricsRegistry()
        self._logger = logger or StructuredLogger()
        self._alerts = alerts

        self._connected: bool = False
        self._circuit_broken: bool = False
        self._queue: List[OkxOrderBookEvent] = []

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._circuit_broken

    def build_subscription_payload(self, channel: str = "books") -> Dict[str, Any]:
        args = [{"channel": channel, "instId": sym} for sym in sorted(self._symbol_map.keys())]
        return {"op": "subscribe", "args": args}

    def on_connect(self) -> None:
        self._connected = True
        for builder in self._order_books.values():
            builder.invalidate("OKX_WS_RECONNECTED_AWAITING_SNAPSHOT", SequenceStatus.BUFFERING)
        self._metrics.set_gauge("arb_ws_connected", 1.0, labels={"venue_id": self.VENUE_ID})

    def on_disconnect(self, reason: str = "PEER_CLOSED") -> None:
        self._connected = False
        for builder in self._order_books.values():
            builder.invalidate(f"OKX_WS_DISCONNECTED:{reason}", SequenceStatus.DISCONNECTED)
        self._metrics.set_gauge("arb_ws_connected", 0.0, labels={"venue_id": self.VENUE_ID})
        if self._alerts:
            self._alerts.trigger(
                "MARKET_DATA_STREAM_DISCONNECTED",
                f"OKX Spot WebSocket disconnected: {reason}",
                TraceContext(venue_id=self.VENUE_ID),
            )

    def on_raw_message(
        self,
        raw_payload: str | Dict[str, Any],
        receive_time_utc: Optional[datetime] = None,
        monotonic_ns: Optional[int] = None,
    ) -> Optional[OkxOrderBookEvent]:
        if self._circuit_broken:
            raise StreamOverloadError("OKX stream manager is circuit-broken due to queue overload")
        if len(self._queue) >= self._max_queue_size:
            self._circuit_broken = True
            self.on_disconnect(reason="QUEUE_OVERLOAD_CIRCUIT_BREAKER")
            raise StreamOverloadError(f"OKX WebSocket queue exceeded {self._max_queue_size}")

        rx_utc = ensure_utc(receive_time_utc) if receive_time_utc else datetime.now(timezone.utc)
        mono_ns = monotonic_ns if monotonic_ns is not None else time.monotonic_ns()

        data = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        if not isinstance(data, dict):
            raise ValueError("OKX WebSocket message must be a JSON object")

        # Ignore subscription acks: {"event": "subscribe", "arg": {...}}
        if "event" in data:
            return None

        arg = data.get("arg", {})
        inst_id_native = str(arg.get("instId", "")).upper()
        if not inst_id_native or inst_id_native not in self._symbol_map:
            raise ValueError(f"Unknown OKX instId '{inst_id_native}' in WebSocket payload")
        canonical_id = self._symbol_map[inst_id_native]

        action = str(data.get("action", "snapshot")).lower()
        items = data.get("data", [])
        if not items:
            return None
        book_item = items[0]

        ts_ms = int(book_item.get("ts", int(rx_utc.timestamp() * 1000)))
        ev_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        seq_id = int(book_item["seqId"])
        prev_seq_id = int(book_item.get("prevSeqId", -1))

        bids = [
            PriceLevel(price=Decimal(str(lvl[0])), quantity=Decimal(str(lvl[1])))
            for lvl in book_item.get("bids", [])
        ]
        asks = [
            PriceLevel(price=Decimal(str(lvl[0])), quantity=Decimal(str(lvl[1])))
            for lvl in book_item.get("asks", [])
        ]

        ev = OkxOrderBookEvent(
            instrument_id=canonical_id,
            venue_symbol=inst_id_native,
            action=action,
            prev_seq_id=prev_seq_id,
            seq_id=seq_id,
            bids=bids,
            asks=asks,
            event_time_utc=ev_utc,
            receive_time_utc=rx_utc,
            monotonic_receive_ns=mono_ns,
        )
        self._queue.append(ev)

        builder = self._order_books.get(canonical_id)
        if builder is not None:
            if action == "snapshot":
                builder.apply_snapshot(
                    bids=bids,
                    asks=asks,
                    last_update_id=seq_id,
                    event_time_utc=ev_utc,
                    receive_time_utc=rx_utc,
                )
            elif action == "update":
                # OKX V5 rule: prevSeqId == last_update_id (or prevSeqId <= last_update_id <= seqId)
                if prev_seq_id == -1 or prev_seq_id != builder.last_update_id:
                    if seq_id <= builder.last_update_id:
                        return ev
                    builder.invalidate(
                        f"OKX_SEQ_GAP: prevSeqId={prev_seq_id} != last_update_id={builder.last_update_id}",
                        SequenceStatus.GAP_DETECTED,
                    )
                    return ev
                builder.apply_diff(
                    first_update_id=prev_seq_id + 1,
                    final_update_id=seq_id,
                    bid_deltas=bids,
                    ask_deltas=asks,
                    event_time_utc=ev_utc,
                    receive_time_utc=rx_utc,
                )
        return ev
