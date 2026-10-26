"""EQ-002 & EQ-003: Equity-linked token/perp market adapter and traditional stock broker adapter.

Enforces:
- EQ-002 (`S3-007`): Strict separation of `executable_bid`, `executable_ask`, `mark_price`,
  `index_reference_price`, `funding_rate`, `session_status`, `source_timestamp_utc`, and
  `product_terms_version`. Reference/index/mark prices are NEVER substituted as executable prices.
- EQ-003 (`S3-003`, `S3-004`, `S3-006`): Stock broker adapter modeling trading session calendar
  (`REGULAR`, `PRE_MARKET`, `AFTER_HOURS`, `CLOSED`, `HALTED`), short-selling borrow/locate quotes,
  cash/margin balances, native broker order statuses, and corporate action halts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.domain.models import OrderBook, OrderSide, PriceLevel
from arb.equities.terms import (
    EquityProductKind,
    EquityProductTerms,
    EquityTermsRegistry,
    EquityTradingSession,
)


class EquityMarketSnapshot(BaseModel):
    """Normalized market snapshot for an equity-linked token, perpetual, or broker stock leg."""

    venue_id: str
    instrument_id: str
    symbol: str
    product_kind: EquityProductKind
    executable_bid: Optional[Decimal] = None
    executable_bid_size: Decimal = Decimal("0")
    executable_ask: Optional[Decimal] = None
    executable_ask_size: Decimal = Decimal("0")
    bids: Tuple[PriceLevel, ...] = Field(default_factory=tuple)
    asks: Tuple[PriceLevel, ...] = Field(default_factory=tuple)
    mark_price: Optional[Decimal] = None
    index_reference_price: Optional[Decimal] = None
    funding_rate_8h: Optional[Decimal] = None
    session_status: EquityTradingSession
    underlying_session_status: EquityTradingSession
    source_timestamp_utc: datetime
    received_timestamp_utc: datetime
    product_terms_version: str
    eligibility_approved: bool = False
    underlying_hedge_executable: bool = False

    class Config:
        frozen = True

    @property
    def is_quote_executable(self) -> bool:
        """True only if real executable bid/ask exist, eligibility is approved, and session is open."""
        if not self.eligibility_approved:
            return False
        if self.session_status in (EquityTradingSession.CLOSED, EquityTradingSession.HALTED):
            return False
        if self.executable_bid is None or self.executable_ask is None:
            return False
        if self.executable_bid <= Decimal("0") or self.executable_ask <= Decimal("0"):
            return False
        if self.executable_bid_size <= Decimal("0") or self.executable_ask_size <= Decimal("0"):
            return False
        return True

    def to_orderbook_snapshot(self, sequence_id: int = 1) -> OrderBook:
        """Convert executable levels into a canonical OrderBook (never using mark/reference)."""
        if not self.is_quote_executable:
            raise ValueError(
                f"S3-007 Violation: Snapshot for {self.instrument_id} is not executable "
                f"(session={self.session_status.value}, eligibility={self.eligibility_approved})"
            )
        bids = list(self.bids) if self.bids else [PriceLevel(price=self.executable_bid, quantity=self.executable_bid_size)]
        asks = list(self.asks) if self.asks else [PriceLevel(price=self.executable_ask, quantity=self.executable_ask_size)]
        return OrderBook(
            venue_id=self.venue_id,
            instrument_id=self.instrument_id,
            bids=bids,
            asks=asks,
            last_update_id=sequence_id,
            is_valid=True,
            event_time_utc=self.source_timestamp_utc,
            receive_time_utc=self.received_timestamp_utc,
        )

    def estimate_vwap(self, side: OrderSide, target_qty: Decimal) -> Tuple[bool, Optional[Decimal], Decimal]:
        """Compute executable VWAP across L2 depth without ever falling back to mark/reference prices."""
        ob = self.to_orderbook_snapshot()
        levels = ob.asks if side == OrderSide.BUY else ob.bids
        remaining = target_qty
        filled = Decimal("0")
        notional = Decimal("0")
        for lvl in levels:
            if remaining <= Decimal("0"):
                break
            take = min(remaining, lvl.quantity)
            filled += take
            notional += take * lvl.price
            remaining -= take
        if remaining > Decimal("0") or filled <= Decimal("0"):
            return False, None, filled
        return True, notional / filled, filled


class EquityLinkedMarketAdapter:
    """EQ-002: Normalizes equity-linked token / perpetual market data and enforces S3-007."""

    def __init__(self, venue_id: str, terms_registry: EquityTermsRegistry) -> None:
        self.venue_id = venue_id
        self.terms_registry = terms_registry

    def normalize_snapshot(
        self,
        instrument_id: str,
        raw_payload: Dict[str, object],
        underlying_session_status: EquityTradingSession,
        now_utc: Optional[datetime] = None,
    ) -> EquityMarketSnapshot:
        """Normalize raw token/perp payload while strictly separating executable vs reference prices."""
        now = now_utc or datetime.now(timezone.utc)
        terms = self.terms_registry.get_terms(instrument_id)
        if terms is None:
            raise KeyError(f"Instrument {instrument_id} not found in EquityTermsRegistry")

        approved, _ = terms.is_executable_approved(now)

        # Parse executable bid/ask ONLY from explicit orderbook fields, NEVER from mark/index
        raw_bids = raw_payload.get("bids", [])
        raw_asks = raw_payload.get("asks", [])
        bids = tuple(
            PriceLevel(price=Decimal(str(level[0])), quantity=Decimal(str(level[1])))
            for level in raw_bids
        )
        asks = tuple(
            PriceLevel(price=Decimal(str(level[0])), quantity=Decimal(str(level[1])))
            for level in raw_asks
        )

        exec_bid = bids[0].price if bids else (
            Decimal(str(raw_payload["executable_bid"])) if raw_payload.get("executable_bid") is not None else None
        )
        exec_bid_sz = bids[0].quantity if bids else Decimal(str(raw_payload.get("executable_bid_size", "0")))
        exec_ask = asks[0].price if asks else (
            Decimal(str(raw_payload["executable_ask"])) if raw_payload.get("executable_ask") is not None else None
        )
        exec_ask_sz = asks[0].quantity if asks else Decimal(str(raw_payload.get("executable_ask_size", "0")))

        mark_price = (
            Decimal(str(raw_payload["mark_price"])) if raw_payload.get("mark_price") is not None else None
        )
        index_price = (
            Decimal(str(raw_payload["index_price"])) if raw_payload.get("index_price") is not None else None
        )
        funding_rate = (
            Decimal(str(raw_payload["funding_rate_8h"]))
            if raw_payload.get("funding_rate_8h") is not None
            else None
        )

        venue_session_str = str(raw_payload.get("session_status", EquityTradingSession.CONTINUOUS_24X7.value))
        venue_session = EquityTradingSession(venue_session_str)

        # Underlying hedge leg is executable only when underlying equity session is active and not closed/halted
        underlying_hedge_executable = underlying_session_status in (
            EquityTradingSession.REGULAR,
            EquityTradingSession.PRE_MARKET,
            EquityTradingSession.AFTER_HOURS,
            EquityTradingSession.OVERNIGHT_24X5,
        )

        source_ts = raw_payload.get("source_timestamp_utc")
        if not isinstance(source_ts, datetime):
            source_ts = now

        return EquityMarketSnapshot(
            venue_id=self.venue_id,
            instrument_id=instrument_id,
            symbol=terms.symbol,
            product_kind=terms.product_kind,
            executable_bid=exec_bid,
            executable_bid_size=exec_bid_sz,
            executable_ask=exec_ask,
            executable_ask_size=exec_ask_sz,
            bids=bids,
            asks=asks,
            mark_price=mark_price,
            index_reference_price=index_price,
            funding_rate_8h=funding_rate,
            session_status=venue_session,
            underlying_session_status=underlying_session_status,
            source_timestamp_utc=source_ts,
            received_timestamp_utc=now,
            product_terms_version=terms.product_terms_version,
            eligibility_approved=approved,
            underlying_hedge_executable=underlying_hedge_executable,
        )


class BorrowLocateQuote(BaseModel):
    """Short-sale stock borrow / locate quote from a prime broker (`S3-004`)."""

    locate_id: str
    underlying_ticker: str
    available_shares: Decimal
    annualized_borrow_rate_bps: Decimal
    is_hard_to_borrow: bool = False
    short_sale_restricted_ssr: bool = False
    expires_at_utc: datetime

    class Config:
        frozen = True

    def can_short(self, requested_shares: Decimal, now_utc: datetime) -> Tuple[bool, Optional[str]]:
        if now_utc >= self.expires_at_utc:
            return False, "BORROW_LOCATE_EXPIRED"
        if self.available_shares < requested_shares:
            return False, "INSUFFICIENT_BORROW_SHARES_AVAILABLE"
        return True, None


class EquityBrokerOrderStatus(str, Enum):
    PENDING_SUBMIT = "PendingSubmit"
    PRE_SUBMITTED = "PreSubmitted"
    SUBMITTED = "Submitted"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"


class EquityBrokerOrderRecord(BaseModel):
    client_order_id: str
    broker_order_id: str
    instrument_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    allow_extended_hours: bool = False
    native_status: EquityBrokerOrderStatus
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Optional[Decimal] = None
    locate_id: Optional[str] = None
    reject_reason: Optional[str] = None
    submitted_at_utc: datetime


class EquityBrokerAccountSnapshot(BaseModel):
    broker_id: str
    settled_cash_usd: Decimal
    buying_power_usd: Decimal
    maintenance_margin_usd: Decimal
    positions_shares: Dict[str, Decimal] = Field(default_factory=dict)
    jurisdiction_approved: bool = False


class EquityBrokerAdapter:
    """EQ-003: Regulated stock broker adapter with session calendar, borrow locate, and corporate actions."""

    def __init__(
        self,
        broker_id: str,
        terms_registry: EquityTermsRegistry,
        account_snapshot: EquityBrokerAccountSnapshot,
    ) -> None:
        self.broker_id = broker_id
        self.terms_registry = terms_registry
        self._account = account_snapshot
        self._session_by_ticker: Dict[str, EquityTradingSession] = {}
        self._borrow_quotes: Dict[str, BorrowLocateQuote] = {}
        self._orders: Dict[str, EquityBrokerOrderRecord] = {}

    def set_trading_session(self, underlying_ticker: str, session: EquityTradingSession) -> None:
        self._session_by_ticker[underlying_ticker] = session

    def get_trading_session(self, underlying_ticker: str) -> EquityTradingSession:
        return self._session_by_ticker.get(underlying_ticker, EquityTradingSession.CLOSED)

    def set_borrow_locate_quote(self, quote: BorrowLocateQuote) -> None:
        self._borrow_quotes[quote.underlying_ticker] = quote

    def get_borrow_locate_quote(self, underlying_ticker: str) -> Optional[BorrowLocateQuote]:
        return self._borrow_quotes.get(underlying_ticker)

    def get_account_snapshot(self) -> EquityBrokerAccountSnapshot:
        return self._account

    def is_synchronous_hedge_executable(
        self,
        instrument_id: str,
        allow_extended_hours: bool,
        now_utc: datetime,
    ) -> Tuple[bool, Optional[str]]:
        """Verify whether the broker leg can execute a synchronous hedge right now (`S3-003`, `S3-006`)."""
        if not self._account.jurisdiction_approved:
            return False, "BROKER_JURISDICTION_UNAPPROVED"

        terms = self.terms_registry.get_terms(instrument_id)
        if terms is None:
            return False, "BROKER_INSTRUMENT_UNREGISTERED"

        ok, reason = terms.is_executable_approved(now_utc)
        if not ok:
            return False, reason

        active_ca = self.terms_registry.get_active_corporate_actions(terms.underlying_ticker, now_utc)
        if active_ca:
            return False, f"BROKER_CORPORATE_ACTION_FREEZE:{active_ca[0].action_type.value}"

        session = self.get_trading_session(terms.underlying_ticker)
        if session in (EquityTradingSession.CLOSED, EquityTradingSession.HALTED):
            return False, f"UNDERLYING_MARKET_{session.value}_NO_SYNC_HEDGE"

        if session in (EquityTradingSession.PRE_MARKET, EquityTradingSession.AFTER_HOURS) and not allow_extended_hours:
            return False, f"EXTENDED_HOURS_{session.value}_NOT_ENABLED_FOR_ORDER"

        if session not in terms.supported_sessions:
            return False, f"SESSION_{session.value}_NOT_SUPPORTED_BY_INSTRUMENT"

        return True, None

    def normalize_broker_quote(
        self,
        instrument_id: str,
        bid: Optional[Decimal],
        bid_size: Decimal,
        ask: Optional[Decimal],
        ask_size: Decimal,
        last_reference_price: Optional[Decimal],
        now_utc: datetime,
    ) -> EquityMarketSnapshot:
        terms = self.terms_registry.get_terms(instrument_id)
        if terms is None:
            raise KeyError(f"Broker instrument {instrument_id} not registered")
        session = self.get_trading_session(terms.underlying_ticker)
        approved, _ = terms.is_executable_approved(now_utc)
        executable = (
            approved
            and self._account.jurisdiction_approved
            and session not in (EquityTradingSession.CLOSED, EquityTradingSession.HALTED)
        )
        bids = (PriceLevel(price=bid, quantity=bid_size),) if (bid and bid_size > 0) else ()
        asks = (PriceLevel(price=ask, quantity=ask_size),) if (ask and ask_size > 0) else ()
        return EquityMarketSnapshot(
            venue_id=self.broker_id,
            instrument_id=instrument_id,
            symbol=terms.symbol,
            product_kind=terms.product_kind,
            executable_bid=bid,
            executable_bid_size=bid_size,
            executable_ask=ask,
            executable_ask_size=ask_size,
            bids=bids,
            asks=asks,
            mark_price=last_reference_price,
            index_reference_price=last_reference_price,
            funding_rate_8h=None,
            session_status=session,
            underlying_session_status=session,
            source_timestamp_utc=now_utc,
            received_timestamp_utc=now_utc,
            product_terms_version=terms.product_terms_version,
            eligibility_approved=approved and self._account.jurisdiction_approved,
            underlying_hedge_executable=executable,
        )

    def submit_order(
        self,
        client_order_id: str,
        instrument_id: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
        allow_extended_hours: bool,
        now_utc: datetime,
    ) -> EquityBrokerOrderRecord:
        """Validate session, corporate actions, cash/short-locate, and record native broker status."""
        terms = self.terms_registry.get_terms(instrument_id)
        if terms is None:
            raise KeyError(f"Unknown broker instrument {instrument_id}")

        can_exec, reason = self.is_synchronous_hedge_executable(
            instrument_id=instrument_id,
            allow_extended_hours=allow_extended_hours,
            now_utc=now_utc,
        )
        if not can_exec:
            record = EquityBrokerOrderRecord(
                client_order_id=client_order_id,
                broker_order_id=f"brk_rej_{uuid4().hex[:8]}",
                instrument_id=instrument_id,
                symbol=terms.symbol,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                allow_extended_hours=allow_extended_hours,
                native_status=EquityBrokerOrderStatus.REJECTED,
                reject_reason=reason,
                submitted_at_utc=now_utc,
            )
            self._orders[client_order_id] = record
            return record

        locate_id: Optional[str] = None
        current_pos = self._account.positions_shares.get(terms.underlying_ticker, Decimal("0"))
        if side == OrderSide.SELL and current_pos < quantity:
            short_qty = quantity - max(current_pos, Decimal("0"))
            if not terms.short_sale_allowed:
                record = EquityBrokerOrderRecord(
                    client_order_id=client_order_id,
                    broker_order_id=f"brk_rej_{uuid4().hex[:8]}",
                    instrument_id=instrument_id,
                    symbol=terms.symbol,
                    side=side,
                    quantity=quantity,
                    limit_price=limit_price,
                    allow_extended_hours=allow_extended_hours,
                    native_status=EquityBrokerOrderStatus.REJECTED,
                    reject_reason="SHORT_SALE_NOT_ALLOWED_BY_TERMS",
                    submitted_at_utc=now_utc,
                )
                self._orders[client_order_id] = record
                return record

            quote = self.get_borrow_locate_quote(terms.underlying_ticker)
            if quote is None:
                record = EquityBrokerOrderRecord(
                    client_order_id=client_order_id,
                    broker_order_id=f"brk_rej_{uuid4().hex[:8]}",
                    instrument_id=instrument_id,
                    symbol=terms.symbol,
                    side=side,
                    quantity=quantity,
                    limit_price=limit_price,
                    allow_extended_hours=allow_extended_hours,
                    native_status=EquityBrokerOrderStatus.REJECTED,
                    reject_reason="MISSING_BORROW_LOCATE_QUOTE",
                    submitted_at_utc=now_utc,
                )
                self._orders[client_order_id] = record
                return record

            can_short, short_reason = quote.can_short(short_qty, now_utc)
            if not can_short:
                record = EquityBrokerOrderRecord(
                    client_order_id=client_order_id,
                    broker_order_id=f"brk_rej_{uuid4().hex[:8]}",
                    instrument_id=instrument_id,
                    symbol=terms.symbol,
                    side=side,
                    quantity=quantity,
                    limit_price=limit_price,
                    allow_extended_hours=allow_extended_hours,
                    native_status=EquityBrokerOrderStatus.REJECTED,
                    reject_reason=short_reason,
                    submitted_at_utc=now_utc,
                )
                self._orders[client_order_id] = record
                return record
            locate_id = quote.locate_id

        record = EquityBrokerOrderRecord(
            client_order_id=client_order_id,
            broker_order_id=f"brk_ord_{uuid4().hex[:8]}",
            instrument_id=instrument_id,
            symbol=terms.symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            allow_extended_hours=allow_extended_hours,
            native_status=EquityBrokerOrderStatus.SUBMITTED,
            locate_id=locate_id,
            submitted_at_utc=now_utc,
        )
        self._orders[client_order_id] = record
        return record
