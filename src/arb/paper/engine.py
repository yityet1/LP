"""[EXEC-003] Paper Matching Engine & End-to-End Simulated Execution."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.costs.engine import CostEngine
from arb.domain.models import (
    BalanceSnapshot,
    CycleState,
    ExecutionPlan,
    Fill,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    PriceType,
)
from arb.execution.fsm import ExecutionOrchestrator
from arb.instruments.registry import InstrumentRegistry
from arb.ledger.store import LedgerStore
from arb.marketdata.orderbook import OrderBookBuilder
from arb.venues.base import AccountAdapter, ErrorCategory, ExchangeError, TradingAdapter


class PaperFillBehavior(str, Enum):
    IMMEDIATE_FULL = "immediate_full"
    PARTIAL_FILL = "partial_fill"
    REJECT_ORDER = "reject_order"
    TIMEOUT_UNKNOWN = "timeout_unknown"


class PaperMatchingConfig(BaseModel):
    behavior: PaperFillBehavior = PaperFillBehavior.IMMEDIATE_FULL
    partial_fill_ratio: Decimal = Decimal("0.50")
    extra_slippage_bps: Decimal = Decimal("0")
    simulated_latency_ms: int = 5
    is_paper_simulation: bool = True


class PaperCycleExecutionResult(BaseModel):
    cycle_id: str
    is_paper_simulation: bool = True
    cycle_state: CycleState
    order_events: List[OrderEvent]
    fills: List[Fill]
    unhedged_base_exposure: Decimal = Decimal("0")
    recovery_triggered: bool = False
    recovery_notes: List[str] = Field(default_factory=list)


class PaperVenueAdapter(TradingAdapter, AccountAdapter):
    """
    [EXEC-003] Implements the exact same `TradingAdapter` & `AccountAdapter` interfaces as live venues,
    enabling strategies, `RiskGateway`, and `ExecutionOrchestrator` to run unmodified in paper mode.
    Every generated order/fill explicitly carries `PAPER_` prefixes so simulated results are never confused with live trades.
    """

    def __init__(
        self,
        venue_id: str,
        registry: InstrumentRegistry,
        cost_engine: CostEngine,
        order_books: Dict[str, OrderBookBuilder],
        initial_balances: Dict[str, Decimal],
        default_config: Optional[PaperMatchingConfig] = None,
    ) -> None:
        self._venue_id = venue_id
        self._registry = registry
        self._cost_engine = cost_engine
        self._order_books = order_books
        self._balances: Dict[str, Decimal] = {k.upper(): v for k, v in initial_balances.items()}
        self._default_config = default_config or PaperMatchingConfig()
        self._per_instrument_config: Dict[str, PaperMatchingConfig] = {}
        self._orders: Dict[str, OrderEvent] = {}
        self._fills: Dict[str, List[Fill]] = {}

    @property
    def venue_id(self) -> str:
        return self._venue_id

    def set_instrument_behavior(self, instrument_id: str, cfg: PaperMatchingConfig) -> None:
        self._per_instrument_config[instrument_id] = cfg

    async def fetch_balances(self) -> List[BalanceSnapshot]:
        now = datetime.now(timezone.utc)
        return [
            BalanceSnapshot(
                venue_id=self._venue_id,
                account_id=f"paper_{self._venue_id}",
                asset=asset,
                total_balance=amt,
                free_balance=amt,
                event_time_utc=now,
                receive_time_utc=now,
            )
            for asset, amt in sorted(self._balances.items())
        ]

    async def fetch_positions(self) -> List[PositionSnapshot]:
        return []

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
        cfg = self._per_instrument_config.get(instrument_id, self._default_config)
        now = datetime.now(timezone.utc)
        paper_ord_id = f"PAPER_ORD_{uuid4().hex[:10]}"

        if cfg.behavior == PaperFillBehavior.TIMEOUT_UNKNOWN:
            # Simulate network timeout where order actually matched on the paper venue in the background!
            ev = OrderEvent(
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                venue_id=self._venue_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=paper_ord_id,
                side=side,
                order_type=order_type,
                status=OrderStatus.UNKNOWN,
                native_venue_status="PAPER_SIMULATED_TIMEOUT_UNKNOWN",
                order_quantity=quantity,
                limit_price=limit_price,
                cumulative_filled_qty=Decimal("0"),
                event_time_utc=now,
                receive_time_utc=now,
            )
            # Store actual filled state on venue side with a distinct event_id so subsequent `query_order` / `fetch_fills` recovers it!
            px = limit_price or Decimal("1")
            fill = Fill(
                fill_id=f"PAPER_FILL_{uuid4().hex[:10]}",
                venue_trade_id=f"PAPER_TRD_{uuid4().hex[:10]}",
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                venue_id=self._venue_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=paper_ord_id,
                side=side,
                fill_qty=quantity,
                fill_price=px,
                fee_amount=quantity * px * Decimal("0.001"),
                fee_asset="USDT",
                event_time_utc=now,
                receive_time_utc=now,
            )
            self._fills[client_order_id] = [fill]
            self._orders[client_order_id] = ev.copy(
                update={
                    "event_id": f"oe_{uuid4().hex[:12]}",
                    "status": OrderStatus.FILLED,
                    "native_venue_status": "PAPER_FILLED",
                    "cumulative_filled_qty": quantity,
                    "average_fill_price": px,
                }
            )
            return ev

        if cfg.behavior == PaperFillBehavior.REJECT_ORDER:
            ev = OrderEvent(
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                venue_id=self._venue_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=paper_ord_id,
                side=side,
                order_type=order_type,
                status=OrderStatus.REJECTED,
                native_venue_status="PAPER_SIMULATED_REJECT",
                order_quantity=quantity,
                limit_price=limit_price,
                cumulative_filled_qty=Decimal("0"),
                event_time_utc=now,
                receive_time_utc=now,
            )
            self._orders[client_order_id] = ev
            self._fills[client_order_id] = []
            return ev

        builder = self._order_books.get(instrument_id)
        rules = self._registry.get_rules(self._venue_id, instrument_id, require_tradable=False)
        exec_qty = (
            rules.quantize_quantity(quantity * cfg.partial_fill_ratio)
            if cfg.behavior == PaperFillBehavior.PARTIAL_FILL
            else quantity
        )

        if builder and builder.is_valid():
            sim = self._cost_engine.simulate_fill(builder.to_order_book(), rules, side, exec_qty)
            fill_px = sim.vwap_fill_price
        else:
            fill_px = limit_price or Decimal("1")

        # Apply configurable extra slippage
        if cfg.extra_slippage_bps != Decimal("0"):
            mult = (
                Decimal("1") + (cfg.extra_slippage_bps / Decimal("10000"))
                if side == OrderSide.BUY
                else Decimal("1") - (cfg.extra_slippage_bps / Decimal("10000"))
            )
            fill_px = fill_px * mult

        # Slippage protection against limit_price
        if limit_price is not None:
            if (side == OrderSide.BUY and fill_px > limit_price) or (
                side == OrderSide.SELL and fill_px < limit_price
            ):
                ev = OrderEvent(
                    cycle_id=cycle_id,
                    strategy_id=strategy_id,
                    venue_id=self._venue_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=paper_ord_id,
                    side=side,
                    order_type=order_type,
                    status=OrderStatus.EXPIRED,
                    native_venue_status="PAPER_IOC_SLIPPAGE_EXCEEDED_LIMIT",
                    order_quantity=quantity,
                    limit_price=limit_price,
                    cumulative_filled_qty=Decimal("0"),
                    event_time_utc=now,
                    receive_time_utc=now,
                )
                self._orders[client_order_id] = ev
                self._fills[client_order_id] = []
                return ev

        fee_est = self._cost_engine.estimate_fees(
            venue_id=self._venue_id,
            notional_in_settlement=exec_qty * fill_px,
            settlement_asset=rules.quote_asset,
            is_maker=False,
        )
        fill = Fill(
            fill_id=f"PAPER_FILL_{uuid4().hex[:10]}",
            venue_trade_id=f"PAPER_TRD_{uuid4().hex[:10]}",
            cycle_id=cycle_id,
            strategy_id=strategy_id,
            venue_id=self._venue_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=paper_ord_id,
            side=side,
            fill_qty=exec_qty,
            fill_price=fill_px,
            price_type=PriceType.ACTUAL_FILL_PRICE,
            fee_amount=fee_est.fee_amount,
            fee_asset=fee_est.fee_asset,
            event_time_utc=now,
            receive_time_utc=now,
        )
        self._fills[client_order_id] = [fill]

        # Update simulated balances
        if side == OrderSide.BUY:
            cost_quote = exec_qty * fill_px + fee_est.fee_in_settlement_asset
            self._balances[rules.quote_asset.upper()] = (
                self._balances.get(rules.quote_asset.upper(), Decimal("0")) - cost_quote
            )
            self._balances[rules.base_asset.upper()] = (
                self._balances.get(rules.base_asset.upper(), Decimal("0")) + exec_qty
            )
        else:
            proceeds_quote = exec_qty * fill_px - fee_est.fee_in_settlement_asset
            self._balances[rules.base_asset.upper()] = (
                self._balances.get(rules.base_asset.upper(), Decimal("0")) - exec_qty
            )
            self._balances[rules.quote_asset.upper()] = (
                self._balances.get(rules.quote_asset.upper(), Decimal("0")) + proceeds_quote
            )

        status = OrderStatus.FILLED if exec_qty >= quantity else OrderStatus.PARTIALLY_FILLED
        ev = OrderEvent(
            cycle_id=cycle_id,
            strategy_id=strategy_id,
            venue_id=self._venue_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=paper_ord_id,
            side=side,
            order_type=order_type,
            status=status,
            native_venue_status=f"PAPER_{status.value.upper()}",
            order_quantity=quantity,
            limit_price=limit_price,
            cumulative_filled_qty=exec_qty,
            average_fill_price=fill_px,
            event_time_utc=now,
            receive_time_utc=now,
        )
        self._orders[client_order_id] = ev
        return ev

    async def query_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        if client_order_id not in self._orders:
            raise ExchangeError(
                venue_id=self._venue_id,
                category=ErrorCategory.DEFINITE_REJECT,
                message=f"Paper order {client_order_id} not found",
                http_status=404,
            )
        stored = self._orders[client_order_id]
        return stored.copy(update={"event_id": f"oe_{uuid4().hex[:12]}"})

    async def cancel_order(
        self,
        instrument_id: str,
        client_order_id: str,
        venue_order_id: Optional[str] = None,
    ) -> OrderEvent:
        ev = self._orders[client_order_id]
        canceled = ev.copy(
            update={
                "event_id": f"oe_{uuid4().hex[:12]}",
                "status": OrderStatus.CANCELED,
                "native_venue_status": "PAPER_CANCELED",
                "cumulative_filled_qty": Decimal("0"),
                "average_fill_price": None,
            }
        )
        self._orders[client_order_id] = canceled
        return canceled

    async def fetch_fills(
        self,
        instrument_id: str,
        client_order_id: Optional[str] = None,
    ) -> List[Fill]:
        if client_order_id:
            return list(self._fills.get(client_order_id, []))
        all_f: List[Fill] = []
        for flist in self._fills.values():
            all_f.extend(flist)
        return all_f


class PaperExecutionSimulator:
    """
    [EXEC-003 / S1-004] Executes a multi-leg `ExecutionPlan` end-to-end against `PaperVenueAdapter` instances
    via `ExecutionOrchestrator` and `LedgerStore`.
    If one leg fills and another leg fails or partially fills (`S1-004`), computes the real unhedged base exposure
    and transitions the cycle into `CycleState.RECOVERY` without fabricating an atomic rollback!
    """

    def __init__(
        self,
        orchestrator: ExecutionOrchestrator,
        ledger: LedgerStore,
        paper_venues: Dict[str, PaperVenueAdapter],
    ) -> None:
        self._orchestrator = orchestrator
        self._ledger = ledger
        self._paper_venues = paper_venues

    async def execute_plan(self, plan: ExecutionPlan) -> PaperCycleExecutionResult:
        order_events: List[OrderEvent] = []
        collected_fills: List[Fill] = []
        net_base_delta = Decimal("0")

        for leg in plan.legs:
            ev = await self._orchestrator.dispatch_leg(plan, leg)
            order_events.append(ev)
            if ev.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                venue_fills = await self._paper_venues[leg.venue_id].fetch_fills(
                    leg.instrument_id, leg.client_order_id
                )
                for f in venue_fills:
                    self._orchestrator.apply_fill_event(f)
                    collected_fills.append(f)
                    if f.side == OrderSide.BUY:
                        net_base_delta += f.fill_qty
                    else:
                        net_base_delta -= f.fill_qty

        # Check if any leg is UNKNOWN, REJECTED, EXPIRED, or PARTIALLY_FILLED resulting in unhedged exposure (S1-004)
        any_unknown = any(e.status == OrderStatus.UNKNOWN for e in order_events)
        all_filled = all(e.status == OrderStatus.FILLED for e in order_events)

        notes: List[str] = []
        recovery_triggered = False
        if any_unknown:
            c_state = CycleState.UNKNOWN
            recovery_triggered = True
            notes.append("CYCLE_HALTED_UNKNOWN_ORDER_AWAITING_RECONCILIATION")
        elif not all_filled or net_base_delta != Decimal("0"):
            c_state = CycleState.RECOVERY
            recovery_triggered = True
            notes.append(
                f"SINGLE_LEG_OR_PARTIAL_FILL_RECOVERY_REQUIRED:real_unhedged_base_exposure={net_base_delta}"
            )
            self._orchestrator._update_cycle_state(plan.cycle_id, CycleState.RECOVERY)
        else:
            c_state = CycleState.SETTLED
            self._orchestrator._update_cycle_state(plan.cycle_id, CycleState.SETTLED)

        return PaperCycleExecutionResult(
            cycle_id=plan.cycle_id,
            is_paper_simulation=True,
            cycle_state=c_state,
            order_events=order_events,
            fills=collected_fills,
            unhedged_base_exposure=net_base_delta,
            recovery_triggered=recovery_triggered,
            recovery_notes=notes,
        )
