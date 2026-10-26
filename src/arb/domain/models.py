"""[CORE-003] Unified domain models and event contracts across all strategies and venues."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field, root_validator, validator


def ensure_utc(dt: datetime) -> datetime:
    """Require timezone-aware datetime and normalize to UTC."""
    if not isinstance(dt, datetime):
        raise ValueError(f"Expected datetime instance, got {type(dt)}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("Naive datetime without timezone is forbidden; all timestamps must be UTC.")
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InstrumentType(str, Enum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    DATED_FUTURE = "dated_future"
    EQUITY = "equity"
    EQUITY_TOKEN = "equity_token"
    OPTION = "option"
    DEX_POOL = "dex_pool"
    WRAPPED_ASSET = "wrapped_asset"


class TradingStatus(str, Enum):
    TRADING = "trading"
    HALTED = "halted"
    BREAK = "break"
    PRE_OPEN = "pre_open"
    POST_CLOSE = "post_close"
    RESEARCH_ONLY = "research_only"


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class PriceType(str, Enum):
    """Section 4.5: Internal APIs MUST explicitly separate price types."""
    BEST_BID = "best_bid"
    BEST_ASK = "best_ask"
    LAST_TRADE_PRICE = "last_trade_price"
    MARK_PRICE = "mark_price"
    INDEX_PRICE = "index_price"
    REFERENCE_PRICE = "reference_price"
    ESTIMATED_FILL_PRICE = "estimated_fill_price"
    ACTUAL_FILL_PRICE = "actual_fill_price"


class DataHealthStatus(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    SEQUENCE_GAP = "sequence_gap"
    CROSS_VENUE_SKEW = "cross_venue_skew"
    DISCONNECTED = "disconnected"
    INVALID = "invalid"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    IOC = "ioc"
    POST_ONLY = "post_only"


class OrderStatus(str, Enum):
    """CEX / Broker unified order lifecycle states (UNKNOWN is never treated as failure)."""
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    UNKNOWN = "unknown"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    FILLED = "filled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    RECONCILING = "reconciling"


class ChainTxStatus(str, Enum):
    """Section 5.3: On-chain transaction lifecycle states (distinct from CEX orders)."""
    CREATED = "created"
    SIMULATED = "simulated"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    REVERTED = "reverted"
    DROPPED = "dropped"
    REPLACED = "replaced"


class OpportunityStatus(str, Enum):
    DETECTED = "detected"
    VALIDATED = "validated"
    COSTED = "costed"
    RISK_CHECKED = "risk_checked"
    PAPER = "paper"
    APPROVED = "approved"
    EXECUTING = "executing"
    RECONCILING = "reconciling"
    SETTLED = "settled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    HALTED = "halted"


class OpportunityDecision(str, Enum):
    REJECT = "reject"
    PAPER = "paper"
    APPROVED = "approved"
    EXPIRED = "expired"


class CycleState(str, Enum):
    PREPARED = "prepared"
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    WORKING = "working"
    RECOVERY = "recovery"
    RECONCILING = "reconciling"
    SETTLED = "settled"
    UNKNOWN = "unknown"
    HALTED = "halted"
    MANUAL_INTERVENTION = "manual_intervention"


class DomainEventBase(BaseModel):
    schema_version: str = Field(default="1.0.0")
    event_time_utc: datetime = Field(default_factory=utc_now)
    receive_time_utc: datetime = Field(default_factory=utc_now)
    monotonic_receive_ns: int = Field(default=0)

    @validator("event_time_utc", "receive_time_utc", pre=True)
    def _validate_utc_time(cls, v: datetime) -> datetime:
        if isinstance(v, str):
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            v = datetime.fromisoformat(v)
        return ensure_utc(v)


class PriceQuote(BaseModel):
    price: Decimal
    price_type: PriceType
    quote_asset: str

    @validator("price")
    def _positive_price(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError(f"Price must be > 0, got {v}")
        return v


class PriceLevel(BaseModel):
    price: Decimal
    quantity: Decimal

    @validator("price")
    def _check_price(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError("PriceLevel price must be > 0")
        return v

    @validator("quantity")
    def _check_qty(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError("PriceLevel quantity cannot be negative")
        return v


class Instrument(DomainEventBase):
    venue_id: str
    instrument_id: str
    venue_symbol: str
    instrument_type: InstrumentType
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_multiplier: Decimal = Decimal("1")
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    min_qty: Decimal = Decimal("0")
    trading_status: TradingStatus = TradingStatus.TRADING
    trading_session: str = "24x7"
    rules_version: str = "v1"
    expiry: Optional[datetime] = None
    strike: Optional[Decimal] = None
    option_type: Optional[OptionType] = None
    reference_index: Optional[str] = None
    funding_interval_hours: Optional[int] = None
    product_terms_version: Optional[str] = None

    @validator("contract_multiplier", "tick_size", "step_size", "min_notional")
    def _positive_specs(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError(f"Instrument specification must be > 0, got {v}")
        return v

    @root_validator
    def _validate_option_fields(cls, values: Dict) -> Dict:
        inst_type = values.get("instrument_type")
        if inst_type == InstrumentType.OPTION:
            if not values.get("expiry") or not values.get("strike") or not values.get("option_type"):
                raise ValueError("Option instrument requires expiry, strike, and option_type")
        return values


class OrderBook(DomainEventBase):
    venue_id: str
    instrument_id: str
    bids: List[PriceLevel] = Field(default_factory=list)
    asks: List[PriceLevel] = Field(default_factory=list)
    last_update_id: int
    is_valid: bool = True
    invalid_reason: Optional[str] = None

    @root_validator
    def _validate_book_integrity(cls, values: Dict) -> Dict:
        bids: List[PriceLevel] = values.get("bids", [])
        asks: List[PriceLevel] = values.get("asks", [])
        # Ensure descending bids and ascending asks
        for i in range(len(bids) - 1):
            if bids[i].price < bids[i + 1].price:
                raise ValueError("OrderBook bids must be sorted in descending order")
        for i in range(len(asks) - 1):
            if asks[i].price > asks[i + 1].price:
                raise ValueError("OrderBook asks must be sorted in ascending order")
        if bids and asks and bids[0].price >= asks[0].price and values.get("is_valid", True):
            raise ValueError(
                f"Crossed order book detected: best_bid ({bids[0].price}) >= best_ask ({asks[0].price})"
            )
        return values

    def best_bid(self) -> Optional[PriceQuote]:
        if not self.is_valid or not self.bids:
            return None
        return PriceQuote(price=self.bids[0].price, price_type=PriceType.BEST_BID, quote_asset="")

    def best_ask(self) -> Optional[PriceQuote]:
        if not self.is_valid or not self.asks:
            return None
        return PriceQuote(price=self.asks[0].price, price_type=PriceType.BEST_ASK, quote_asset="")


class MarketSnapshot(DomainEventBase):
    snapshot_id: str = Field(default_factory=lambda: f"snap_{uuid4().hex[:12]}")
    venue_id: str
    instrument_id: str
    quote_asset: str
    bid_levels: List[PriceLevel] = Field(default_factory=list)
    ask_levels: List[PriceLevel] = Field(default_factory=list)
    last_trade_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    index_price: Optional[Decimal] = None
    reference_price: Optional[Decimal] = None
    sequence_id: int
    data_health: DataHealthStatus = DataHealthStatus.HEALTHY
    book_age_ms: int = 0
    funding_rate: Optional[Decimal] = None
    next_funding_time: Optional[datetime] = None
    gas_estimate_wei: Optional[int] = None
    block_number: Optional[int] = None
    finality_status: Optional[str] = None

    def get_executable_price(self, side: OrderSide) -> Decimal:
        """Strict guard: never allow mark_price, index_price, or last_trade_price as executable price."""
        if self.data_health != DataHealthStatus.HEALTHY:
            raise ValueError(f"Cannot get executable price from unhealthy snapshot ({self.data_health})")
        if side == OrderSide.BUY:
            if not self.ask_levels:
                raise ValueError("No executable ask levels available for BUY")
            return self.ask_levels[0].price
        else:
            if not self.bid_levels:
                raise ValueError("No executable bid levels available for SELL")
            return self.bid_levels[0].price


class OpportunityLeg(BaseModel):
    venue_id: str
    instrument_id: str
    side: OrderSide
    target_quantity: Decimal
    executable_price: Decimal
    executable_price_type: PriceType
    available_depth_qty: Decimal
    expected_fee: Decimal
    fee_asset: str
    rules_version: str

    @validator("executable_price_type")
    def _enforce_executable_price_type(cls, v: PriceType, values: Dict) -> PriceType:
        allowed = {
            PriceType.BEST_BID,
            PriceType.BEST_ASK,
            PriceType.ESTIMATED_FILL_PRICE,
        }
        if v not in allowed:
            raise ValueError(
                f"Leg executable_price_type cannot be '{v.value}'! "
                "Mark, index, reference, or last trade prices are strictly forbidden for execution."
            )
        side = values.get("side")
        if side == OrderSide.BUY and v == PriceType.BEST_BID:
            raise ValueError("BUY leg cannot use BEST_BID as executable price (must use ASK or ESTIMATED_FILL_PRICE)")
        if side == OrderSide.SELL and v == PriceType.BEST_ASK:
            raise ValueError("SELL leg cannot use BEST_ASK as executable price (must use BID or ESTIMATED_FILL_PRICE)")
        return v


class Opportunity(DomainEventBase):
    opportunity_id: str = Field(default_factory=lambda: f"opp_{uuid4().hex[:12]}")
    strategy_id: str
    strategy_version: str
    status: OpportunityStatus = OpportunityStatus.DETECTED
    legs: List[OpportunityLeg]
    input_snapshot_ids: List[str]
    settlement_asset: str
    gross_edge: Decimal
    fees: Decimal
    slippage: Decimal = Decimal("0")
    funding_or_carry: Decimal = Decimal("0")
    gas_or_bridge_cost: Decimal = Decimal("0")
    expected_net_pnl: Decimal
    net_edge_bps: Decimal
    capacity: Decimal
    risk_score: Decimal = Decimal("0")
    expires_at: datetime
    decision: OpportunityDecision = OpportunityDecision.PAPER
    reject_reason_codes: List[str] = Field(default_factory=list)

    @validator("expires_at", pre=True)
    def _utc_expires(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class ExecutionPlanLeg(BaseModel):
    client_order_id: str
    venue_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType = OrderType.IOC
    quantity: Decimal
    limit_price: Decimal
    max_slippage_bps: Decimal
    rules_version: str


class ExecutionPlan(DomainEventBase):
    cycle_id: str = Field(default_factory=lambda: f"cyc_{uuid4().hex[:12]}")
    opportunity_id: str
    strategy_id: str
    strategy_version: str
    execution_mode: str = "paper"
    state: CycleState = CycleState.PREPARED
    legs: List[ExecutionPlanLeg]
    reserved_asset: str
    reserved_amount: Decimal
    max_unhedged_notional: Decimal
    timeout_ms: int = 3000
    config_version_hash: str


class OrderEvent(DomainEventBase):
    event_id: str = Field(default_factory=lambda: f"oe_{uuid4().hex[:12]}")
    cycle_id: str
    strategy_id: str
    venue_id: str
    instrument_id: str
    client_order_id: str
    venue_order_id: Optional[str] = None
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    native_venue_status: str
    order_quantity: Decimal
    limit_price: Optional[Decimal] = None
    cumulative_filled_qty: Decimal = Decimal("0")
    average_fill_price: Optional[Decimal] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class Fill(DomainEventBase):
    fill_id: str
    venue_trade_id: str
    cycle_id: str
    strategy_id: str
    venue_id: str
    instrument_id: str
    client_order_id: str
    venue_order_id: str
    side: OrderSide
    fill_qty: Decimal
    fill_price: Decimal
    price_type: PriceType = PriceType.ACTUAL_FILL_PRICE
    fee_amount: Decimal
    fee_asset: str
    is_maker: bool = False

    @validator("fill_qty", "fill_price")
    def _positive_fill(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError("Fill quantity and price must be > 0")
        return v


class BalanceSnapshot(DomainEventBase):
    snapshot_id: str = Field(default_factory=lambda: f"bal_{uuid4().hex[:12]}")
    venue_id: str
    account_id: str
    asset: str
    total_balance: Decimal
    free_balance: Decimal
    locked_balance: Decimal = Decimal("0")
    reserved_balance: Decimal = Decimal("0")
    in_transit_balance: Decimal = Decimal("0")

    @root_validator
    def _validate_balance_components(cls, values: Dict) -> Dict:
        total = values.get("total_balance", Decimal("0"))
        free = values.get("free_balance", Decimal("0"))
        locked = values.get("locked_balance", Decimal("0"))
        reserved = values.get("reserved_balance", Decimal("0"))
        in_transit = values.get("in_transit_balance", Decimal("0"))
        if any(x < Decimal("0") for x in (total, free, locked, reserved, in_transit)):
            raise ValueError("Balance components cannot be negative")
        return values

    @property
    def allocatable_balance(self) -> Decimal:
        """In-transit, locked, and reserved funds are NEVER counted as free allocatable balance."""
        avail = self.free_balance - self.reserved_balance
        return max(Decimal("0"), avail)


class PositionSnapshot(DomainEventBase):
    position_id: str = Field(default_factory=lambda: f"pos_{uuid4().hex[:12]}")
    venue_id: str
    account_id: str
    instrument_id: str
    position_side: str = "NET"
    quantity: Decimal
    contract_multiplier: Decimal = Decimal("1")
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    margin_used: Decimal = Decimal("0")
    liquidation_price: Optional[Decimal] = None


class ChainTransaction(DomainEventBase):
    transaction_id: str = Field(default_factory=lambda: f"ctx_{uuid4().hex[:12]}")
    cycle_id: str
    strategy_id: str
    chain_id: int
    tx_hash: Optional[str] = None
    sender_address: str
    target_contract: str
    nonce: int
    status: ChainTxStatus = ChainTxStatus.CREATED
    gas_limit: int
    max_fee_per_gas_wei: int
    gas_used: Optional[int] = None
    block_number: Optional[int] = None
    block_hash: Optional[str] = None
    confirmations: int = 0
    required_confirmations: int = 1
