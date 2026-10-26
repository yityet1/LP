"""[MD-003] Official Sequence-Synchronized Local L2 OrderBook Builder."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from arb.domain.models import OrderBook, OrderSide, PriceLevel, ensure_utc


class SequenceStatus(str, Enum):
    UNINITIALIZED = "uninitialized"
    BUFFERING = "buffering"
    SYNCHRONIZED = "synchronized"
    GAP_DETECTED = "gap_detected"
    DISCONNECTED = "disconnected"


class DepthSliceResult(BaseModel):
    side: OrderSide
    requested_qty: Decimal
    filled_qty: Decimal
    unfilled_qty: Decimal
    vwap_price: Optional[Decimal] = None
    total_quote_notional: Decimal = Decimal("0")
    levels_consumed: int = 0
    is_fully_filled: bool = False


class OrderBookBuilder:
    """
    [MD-003] Maintains a local L2 order book strictly adhering to the official Binance diff-depth synchronization state machine:
    1. Buffer incoming WebSocket diff events (`U` = first_update_id, `u` = final_update_id).
    2. Apply REST depth snapshot (`lastUpdateId`).
    3. Drop buffered diff events where `u <= lastUpdateId`.
    4. The first processed diff event after snapshot MUST satisfy `U <= lastUpdateId + 1 <= u`
       (or `U == lastUpdateId + 1` if snapshot captured exact boundary).
    5. Every subsequent diff event MUST satisfy:
       - Ignore stale/duplicate events where `u <= self.last_update_id`.
       - If `U > self.last_update_id + 1`: a sequence gap occurred! Immediately mark book `INVALID`
         and transition to `SequenceStatus.GAP_DETECTED`.
       - Only a fresh `apply_snapshot()` followed by valid diff alignment can restore validity.
    6. Level update with quantity == 0 deletes the price level; bids sorted descending, asks sorted ascending.
    """

    def __init__(self, venue_id: str, instrument_id: str) -> None:
        self.venue_id = venue_id
        self.instrument_id = instrument_id
        self._bids: Dict[Decimal, Decimal] = {}
        self._asks: Dict[Decimal, Decimal] = {}
        self._last_update_id: int = 0
        self._is_valid: bool = False
        self._invalid_reason: Optional[str] = "UNINITIALIZED"
        self._sequence_status: SequenceStatus = SequenceStatus.UNINITIALIZED
        self._awaiting_first_diff_after_snapshot: bool = False
        self._buffered_diffs: List[Tuple[int, int, List[ PriceLevel ], List[ PriceLevel ], datetime, datetime]] = []
        self._last_event_time_utc: datetime = datetime.now(timezone.utc)
        self._last_receive_time_utc: datetime = datetime.now(timezone.utc)

    @property
    def last_update_id(self) -> int:
        return self._last_update_id

    @property
    def sequence_status(self) -> SequenceStatus:
        return self._sequence_status

    @property
    def invalid_reason(self) -> Optional[str]:
        return self._invalid_reason

    @property
    def last_event_time_utc(self) -> datetime:
        return self._last_event_time_utc

    @property
    def last_receive_time_utc(self) -> datetime:
        return self._last_receive_time_utc

    def is_valid(self) -> bool:
        return self._is_valid and self._sequence_status == SequenceStatus.SYNCHRONIZED

    def invalidate(self, reason: str, status: SequenceStatus = SequenceStatus.DISCONNECTED) -> None:
        """Immediately mark order book INVALID on stream disconnect, overload, or sequence gap."""
        self._is_valid = False
        self._invalid_reason = reason
        self._sequence_status = status
        self._awaiting_first_diff_after_snapshot = False
        self._buffered_diffs.clear()

    def apply_snapshot(
        self,
        bids: List[PriceLevel],
        asks: List[PriceLevel],
        last_update_id: int,
        event_time_utc: Optional[datetime] = None,
        receive_time_utc: Optional[datetime] = None,
    ) -> bool:
        """
        Load REST depth snapshot and replay any buffered WebSocket diffs according to official rules.
        """
        if last_update_id <= 0:
            self.invalidate("INVALID_SNAPSHOT_UPDATE_ID", SequenceStatus.GAP_DETECTED)
            return False

        self._bids = {lvl.price: lvl.quantity for lvl in bids if lvl.quantity > Decimal("0")}
        self._asks = {lvl.price: lvl.quantity for lvl in asks if lvl.quantity > Decimal("0")}
        self._last_update_id = last_update_id

        now = datetime.now(timezone.utc)
        self._last_event_time_utc = ensure_utc(event_time_utc) if event_time_utc else now
        self._last_receive_time_utc = ensure_utc(receive_time_utc) if receive_time_utc else now

        # Check crossed book in snapshot itself
        if self._bids and self._asks and max(self._bids.keys()) >= min(self._asks.keys()):
            self.invalidate("CROSSED_BOOK_IN_SNAPSHOT", SequenceStatus.GAP_DETECTED)
            return False

        self._is_valid = True
        self._invalid_reason = None
        self._sequence_status = SequenceStatus.SYNCHRONIZED
        self._awaiting_first_diff_after_snapshot = True

        # Replay buffered diffs if any were collected while fetching snapshot
        if self._buffered_diffs:
            buffered = list(self._buffered_diffs)
            self._buffered_diffs.clear()
            for first_u, final_u, b_deltas, a_deltas, ev_t, rx_t in buffered:
                if final_u <= self._last_update_id:
                    continue
                ok = self.apply_diff(
                    first_update_id=first_u,
                    final_update_id=final_u,
                    bid_deltas=b_deltas,
                    ask_deltas=a_deltas,
                    event_time_utc=ev_t,
                    receive_time_utc=rx_t,
                )
                if not ok:
                    return False

        return self._is_valid

    def buffer_or_apply_diff(
        self,
        first_update_id: int,
        final_update_id: int,
        bid_deltas: List[PriceLevel],
        ask_deltas: List[PriceLevel],
        event_time_utc: Optional[datetime] = None,
        receive_time_utc: Optional[datetime] = None,
    ) -> bool:
        """Buffer diff if snapshot has not yet been applied; otherwise apply directly."""
        now = datetime.now(timezone.utc)
        ev_t = ensure_utc(event_time_utc) if event_time_utc else now
        rx_t = ensure_utc(receive_time_utc) if receive_time_utc else now

        if self._sequence_status in (SequenceStatus.UNINITIALIZED, SequenceStatus.BUFFERING):
            self._sequence_status = SequenceStatus.BUFFERING
            self._buffered_diffs.append((first_update_id, final_update_id, bid_deltas, ask_deltas, ev_t, rx_t))
            return False

        return self.apply_diff(
            first_update_id=first_update_id,
            final_update_id=final_update_id,
            bid_deltas=bid_deltas,
            ask_deltas=ask_deltas,
            event_time_utc=ev_t,
            receive_time_utc=rx_t,
        )

    def apply_diff(
        self,
        first_update_id: int,
        final_update_id: int,
        bid_deltas: List[PriceLevel],
        ask_deltas: List[PriceLevel],
        event_time_utc: Optional[datetime] = None,
        receive_time_utc: Optional[datetime] = None,
    ) -> bool:
        """
        Apply a single L2 diff update `[U, u]`.
        - Drop stale/duplicate updates (`u <= lastUpdateId`).
        - First diff after snapshot must satisfy `U <= lastUpdateId + 1 <= u`.
        - Subsequent diffs must satisfy `U <= lastUpdateId + 1` (no gap). Any `U > lastUpdateId + 1` immediately invalidates the book!
        """
        if not self._is_valid or self._sequence_status != SequenceStatus.SYNCHRONIZED:
            return False

        if first_update_id > final_update_id:
            self.invalidate(
                f"MALFORMED_DIFF_RANGE: U({first_update_id}) > u({final_update_id})",
                SequenceStatus.GAP_DETECTED,
            )
            return False

        # Step 3 / Duplicate or old event: ignore safely without invalidating
        if final_update_id <= self._last_update_id:
            return True

        target_next_id = self._last_update_id + 1
        if self._awaiting_first_diff_after_snapshot:
            # Official Binance rule: U <= lastUpdateId + 1 AND u >= lastUpdateId + 1
            if first_update_id > target_next_id:
                self.invalidate(
                    f"SNAPSHOT_SYNC_GAP: first diff U={first_update_id} > snapshot+1 ({target_next_id})",
                    SequenceStatus.GAP_DETECTED,
                )
                return False
            self._awaiting_first_diff_after_snapshot = False
        else:
            # Strict sequential continuity check
            if first_update_id > target_next_id:
                self.invalidate(
                    f"SEQUENCE_GAP: expected U <= {target_next_id}, got U={first_update_id}",
                    SequenceStatus.GAP_DETECTED,
                )
                return False

        # Apply price level deltas (qty == 0 deletes level)
        for lvl in bid_deltas:
            if lvl.quantity == Decimal("0"):
                self._bids.pop(lvl.price, None)
            else:
                self._bids[lvl.price] = lvl.quantity

        for lvl in ask_deltas:
            if lvl.quantity == Decimal("0"):
                self._asks.pop(lvl.price, None)
            else:
                self._asks[lvl.price] = lvl.quantity

        # Check crossed book after applying diff
        if self._bids and self._asks and max(self._bids.keys()) >= min(self._asks.keys()):
            self.invalidate(
                f"CROSSED_BOOK_AFTER_DIFF: best_bid={max(self._bids.keys())} >= best_ask={min(self._asks.keys())}",
                SequenceStatus.GAP_DETECTED,
            )
            return False

        self._last_update_id = final_update_id
        now = datetime.now(timezone.utc)
        self._last_event_time_utc = ensure_utc(event_time_utc) if event_time_utc else now
        self._last_receive_time_utc = ensure_utc(receive_time_utc) if receive_time_utc else now
        return True

    def depth(self, side: OrderSide, quantity: Decimal) -> DepthSliceResult:
        """
        Simulate walking the book to fill `quantity` of base asset.
        - If side == BUY, walks ASK levels from lowest to highest.
        - If side == SELL, walks BID levels from highest to lowest.
        """
        if not self.is_valid():
            raise RuntimeError(
                f"Cannot compute depth on invalid order book ({self.venue_id}:{self.instrument_id}, reason={self._invalid_reason})"
            )
        if quantity <= Decimal("0"):
            raise ValueError(f"Requested depth quantity must be > 0, got {quantity}")

        levels = (
            sorted(self._asks.items(), key=lambda x: x[0])
            if side == OrderSide.BUY
            else sorted(self._bids.items(), key=lambda x: x[0], reverse=True)
        )

        remaining = quantity
        filled = Decimal("0")
        total_quote = Decimal("0")
        consumed = 0

        for px, avail_qty in levels:
            if remaining <= Decimal("0"):
                break
            take = min(remaining, avail_qty)
            filled += take
            total_quote += take * px
            remaining -= take
            consumed += 1

        vwap = (total_quote / filled) if filled > Decimal("0") else None
        return DepthSliceResult(
            side=side,
            requested_qty=quantity,
            filled_qty=filled,
            unfilled_qty=remaining,
            vwap_price=vwap,
            total_quote_notional=total_quote,
            levels_consumed=consumed,
            is_fully_filled=(remaining == Decimal("0")),
        )

    def to_order_book(self, max_levels: int = 50) -> OrderBook:
        sorted_bids = [
            PriceLevel(price=px, quantity=qty)
            for px, qty in sorted(self._bids.items(), key=lambda x: x[0], reverse=True)[:max_levels]
        ]
        sorted_asks = [
            PriceLevel(price=px, quantity=qty)
            for px, qty in sorted(self._asks.items(), key=lambda x: x[0])[:max_levels]
        ]
        return OrderBook(
            venue_id=self.venue_id,
            instrument_id=self.instrument_id,
            bids=sorted_bids,
            asks=sorted_asks,
            last_update_id=self._last_update_id,
            is_valid=self.is_valid(),
            invalid_reason=self._invalid_reason,
            event_time_utc=self._last_event_time_utc,
            receive_time_utc=self._last_receive_time_utc,
        )
