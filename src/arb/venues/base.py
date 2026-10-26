"""[CORE-004] Venue Adapter layered interfaces and unified error taxonomy."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel

from arb.domain.models import (
    BalanceSnapshot,
    Fill,
    Instrument,
    MarketSnapshot,
    OrderBook,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
)


class ErrorCategory(str, Enum):
    """Unified error taxonomy across CEX, Broker, and DEX adapters."""
    DEFINITE_REJECT = "definite_reject"          # Venue explicitly rejected prior to matching (no state change)
    RATE_LIMITED = "rate_limited"                # HTTP 429 / backoff required
    IP_BANNED = "ip_banned"                      # HTTP 418 / immediate venue circuit breaker
    AUTH_PERMISSION = "auth_permission"          # HTTP 401/403 / invalid key or missing trading scope
    TRANSIENT_NETWORK = "transient_network"      # Read-only safe to retry; write operations become UNKNOWN!
    UNKNOWN_ORDER_STATE = "unknown_order_state"  # Timeout/5xx on order placement/cancel: NEVER treat as definite fail!
    INVALID_RESPONSE = "invalid_response"        # Schema mismatch


class ExchangeError(Exception):
    """Structured venue adapter exception preserving native venue error codes and retry semantics."""

    def __init__(
        self,
        venue_id: str,
        category: ErrorCategory,
        message: str,
        http_status: Optional[int] = None,
        native_code: Optional[str] = None,
        is_write_operation: bool = False,
        retry_after_ms: Optional[int] = None,
    ) -> None:
        super().__init__(f"[{venue_id}:{category.value}] {message} (status={http_status}, code={native_code})")
        self.venue_id = venue_id
        self.category = category
        self.message = message
        self.http_status = http_status
        self.native_code = native_code
        self.is_write_operation = is_write_operation
        self.retry_after_ms = retry_after_ms

    @property
    def is_definite_failure(self) -> bool:
        """Only explicit pre-execution rejections or auth errors are definite failures."""
        if self.is_write_operation and self.category in (
            ErrorCategory.UNKNOWN_ORDER_STATE,
            ErrorCategory.TRANSIENT_NETWORK,
        ):
            return False
        return self.category in (ErrorCategory.DEFINITE_REJECT, ErrorCategory.AUTH_PERMISSION)

    @property
    def requires_reconciliation(self) -> bool:
        """Write operations with timeout/5xx/network drop MUST transition order to UNKNOWN and reconcile."""
        return self.is_write_operation and self.category in (
            ErrorCategory.UNKNOWN_ORDER_STATE,
            ErrorCategory.TRANSIENT_NETWORK,
        )

    @property
    def is_retryable_readonly(self) -> bool:
        if self.is_write_operation:
            return False
        return self.category in (ErrorCategory.TRANSIENT_NETWORK, ErrorCategory.RATE_LIMITED)


class OrderSubmissionResult(BaseModel):
    venue_id: str
    client_order_id: str
    venue_order_id: Optional[str] = None
    status: OrderStatus
    native_venue_status: str
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    error_code: Optional[str] = None


class InstrumentMetadataAdapter(ABC):
    """Layer 1: Fetch normalized instrument trading rules and metadata."""

    @property
    @abstractmethod
    def venue_id(self) -> str:
        pass

    @abstractmethod
    async def get_server_time(self) -> datetime:
        pass

    @abstractmethod
    async def fetch_instruments(self) -> List[Instrument]:
        pass


class MarketDataAdapter(ABC):
    """Layer 2: Read-only market data snapshot and order book adapter."""

    @property
    @abstractmethod
    def venue_id(self) -> str:
        pass

    @abstractmethod
    async def fetch_order_book(self, instrument_id: str, depth: int = 20) -> OrderBook:
        pass

    @abstractmethod
    async def fetch_market_snapshot(self, instrument_id: str) -> MarketSnapshot:
        pass


class AccountAdapter(ABC):
    """Layer 3: Account balances and derivative/broker position snapshots."""

    @property
    @abstractmethod
    def venue_id(self) -> str:
        pass

    @abstractmethod
    async def fetch_balances(self) -> List[BalanceSnapshot]:
        pass

    @abstractmethod
    async def fetch_positions(self) -> List[PositionSnapshot]:
        pass


class TradingAdapter(ABC):
    """Layer 4: Order lifecycle execution and reconciliation queries."""

    @property
    @abstractmethod
    def venue_id(self) -> str:
        pass

    @abstractmethod
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
        """Submit order. On network timeout or HTTP 5xx, MUST return OrderStatus.UNKNOWN (never raise definite fail)."""
        pass

    @abstractmethod
    async def query_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        pass

    @abstractmethod
    async def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        pass

    @abstractmethod
    async def fetch_fills(
        self,
        instrument_id: str,
        client_order_id: Optional[str] = None,
    ) -> List[Fill]:
        pass
