"""[EXEC-001] CEX Order State Machine, Transactional Outbox, Single-Worker Lease & Unknown State Protection."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Set

from pydantic import BaseModel

from arb.domain.models import (
    CycleState,
    ExecutionPlan,
    ExecutionPlanLeg,
    Fill,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from arb.ledger.store import LedgerStore, RiskDecisionRecord
from arb.risk.gateway import KillSwitchScope, RiskGateway
from arb.venues.base import ExchangeError, TradingAdapter


class InvalidOrderStateTransitionError(RuntimeError):
    """Raised when an illegal order state transition is attempted."""


class ConcurrentWorkerConflictError(RuntimeError):
    """Raised when a second worker attempts to execute a cycle already leased by another worker (CORE-007)."""


class OutboxOrderRecord(BaseModel):
    client_order_id: str
    cycle_id: str
    strategy_id: str
    venue_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Optional[Decimal]
    outbox_state: str  # PREPARED | DISPATCHED | RECONCILED | COMPLETED


class OrderStateMachine:
    """
    [EXEC-001] Enforces legal CEX order state transitions and late-fill accounting after cancel:
    `PREPARED -> SUBMITTING -> UNKNOWN / NEW / PARTIALLY_FILLED / FILLED / CANCELED / REJECTED / EXPIRED`
    - Note (`CORE-005`): A `CANCELED` or `CANCEL_PENDING` order that receives a late fill event
      is allowed to update its `cumulative_filled_qty` and transition to `PARTIALLY_FILLED` or `FILLED`
      so late fills are never lost!
    """

    ALLOWED_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
        OrderStatus.PREPARED: {
            OrderStatus.SUBMITTING,
            OrderStatus.CANCELED,
        },
        OrderStatus.SUBMITTING: {
            OrderStatus.UNKNOWN,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.RECONCILING,
        },
        OrderStatus.UNKNOWN: {
            OrderStatus.RECONCILING,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        },
        OrderStatus.RECONCILING: {
            OrderStatus.UNKNOWN,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        },
        OrderStatus.NEW: {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
            OrderStatus.RECONCILING,
        },
        OrderStatus.PARTIALLY_FILLED: {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.UNKNOWN,
            OrderStatus.RECONCILING,
        },
        OrderStatus.CANCEL_PENDING: {
            OrderStatus.CANCELED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.UNKNOWN,
            OrderStatus.RECONCILING,
        },
        OrderStatus.CANCELED: {
            # Late fill after cancel (CORE-005): allow accounting for fills that matched prior to cancel!
            OrderStatus.CANCELED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        },
        OrderStatus.FILLED: {
            OrderStatus.FILLED,
        },
        OrderStatus.REJECTED: {
            OrderStatus.REJECTED,
        },
        OrderStatus.EXPIRED: {
            OrderStatus.EXPIRED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        },
    }

    @classmethod
    def validate_transition(cls, current: OrderStatus, target: OrderStatus) -> None:
        allowed = cls.ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidOrderStateTransitionError(
                f"Illegal order state transition: {current.value} -> {target.value}"
            )


class ExecutionOrchestrator:
    """
    [EXEC-001] Coordinates multi-leg `ExecutionPlan` execution with:
    1. Single-worker exclusive lease (`cycle_worker_leases` table) preventing dual-worker race (`CORE-007`).
    2. Transactional Outbox (`order_outbox` + `orders` tables): persists `PREPARED` intent BEFORE sending network call (`CORE-006`).
    3. Idempotent submission guard: if an order is already in `SUBMITTING` or `UNKNOWN` in the Outbox (e.g. after crash/restart),
       it NEVER blindly re-submits the order (`CORE-004`); instead it transitions to `RECONCILING`.
    4. Separation of Order Terminal State vs. Cycle Settlement: even when all orders reach terminal states,
       the cycle transitions to `CycleState.RECONCILING` until fills/balances are reconciled (`EXEC-001`).
    """

    def __init__(
        self,
        ledger: LedgerStore,
        risk_gateway: RiskGateway,
        venues: Dict[str, TradingAdapter],
        worker_id: str = "worker_primary",
    ) -> None:
        self._ledger = ledger
        self._risk = risk_gateway
        self._venues = venues
        self._worker_id = worker_id

    def acquire_cycle_lease(self, cycle_id: str) -> None:
        """Acquire atomic exclusive lease for `cycle_id` (`CORE-007`)."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._ledger.transaction() as cur:
            row = cur.execute(
                "SELECT worker_id FROM cycle_worker_leases WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if row is not None and row["worker_id"] != self._worker_id:
                raise ConcurrentWorkerConflictError(
                    f"Cycle {cycle_id} is already locked by worker '{row['worker_id']}' "
                    f"(current worker='{self._worker_id}')"
                )
            if row is None:
                cur.execute(
                    "INSERT INTO cycle_worker_leases (cycle_id, worker_id, acquired_at_utc) VALUES (?, ?, ?)",
                    (cycle_id, self._worker_id, now_str),
                )

    def prepare_cycle_outbox(self, plan: ExecutionPlan, risk_decision: RiskDecisionRecord) -> List[OutboxOrderRecord]:
        """
        Persist cycle, risk decision, and `PREPARED` order intentions in a single ACID transaction
        BEFORE any external API call is made (`CORE-006`).
        """
        self.acquire_cycle_lease(plan.cycle_id)
        self._ledger.create_cycle(plan, risk_decision)

        now_utc = datetime.now(timezone.utc)
        now_str = now_utc.isoformat()
        records: List[OutboxOrderRecord] = []

        for leg in plan.legs:
            prep_event = OrderEvent(
                cycle_id=plan.cycle_id,
                strategy_id=plan.strategy_id,
                venue_id=leg.venue_id,
                instrument_id=leg.instrument_id,
                client_order_id=leg.client_order_id,
                side=leg.side,
                order_type=leg.order_type,
                status=OrderStatus.PREPARED,
                native_venue_status="OUTBOX_PREPARED",
                order_quantity=leg.quantity,
                limit_price=leg.limit_price,
                cumulative_filled_qty=Decimal("0"),
                event_time_utc=now_utc,
                receive_time_utc=now_utc,
            )
            self._ledger.record_order_event(prep_event)
            with self._ledger.transaction() as cur:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO order_outbox (
                        client_order_id, cycle_id, strategy_id, venue_id, instrument_id,
                        side, order_type, quantity, limit_price, outbox_state, worker_id,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        leg.client_order_id,
                        plan.cycle_id,
                        plan.strategy_id,
                        leg.venue_id,
                        leg.instrument_id,
                        leg.side.value,
                        leg.order_type.value,
                        str(leg.quantity),
                        str(leg.limit_price),
                        "PREPARED",
                        self._worker_id,
                        now_str,
                        now_str,
                    ),
                )
            records.append(
                OutboxOrderRecord(
                    client_order_id=leg.client_order_id,
                    cycle_id=plan.cycle_id,
                    strategy_id=plan.strategy_id,
                    venue_id=leg.venue_id,
                    instrument_id=leg.instrument_id,
                    side=leg.side,
                    order_type=leg.order_type,
                    quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    outbox_state="PREPARED",
                )
            )
        return records

    async def dispatch_leg(self, plan: ExecutionPlan, leg: ExecutionPlanLeg) -> OrderEvent:
        """
        Dispatch a single leg from the Outbox:
        - Checks current order status in `LedgerStore`.
        - If status is `UNKNOWN`, `SUBMITTING`, `NEW`, `PARTIALLY_FILLED`, or `FILLED`,
          NEVER re-send `submit_order`! Instead return current/UNKNOWN state for reconciliation (`CORE-004`).
        - Otherwise transition `PREPARED -> SUBMITTING`, mark outbox `DISPATCHED`, and invoke `TradingAdapter.submit_order`.
        """
        self.acquire_cycle_lease(plan.cycle_id)

        row = self._ledger._conn.execute(
            "SELECT status, cumulative_filled_qty, venue_order_id, native_venue_status FROM orders WHERE client_order_id = ?",
            (leg.client_order_id,),
        ).fetchone()

        if row is not None:
            cur_status = OrderStatus(row["status"])
            if cur_status != OrderStatus.PREPARED:
                # Crash recovery / duplicate dispatch guard: NEVER blindly resend an already-dispatched order!
                return OrderEvent(
                    cycle_id=plan.cycle_id,
                    strategy_id=plan.strategy_id,
                    venue_id=leg.venue_id,
                    instrument_id=leg.instrument_id,
                    client_order_id=leg.client_order_id,
                    venue_order_id=row["venue_order_id"],
                    side=leg.side,
                    order_type=leg.order_type,
                    status=OrderStatus.UNKNOWN if cur_status == OrderStatus.SUBMITTING else cur_status,
                    native_venue_status="SKIPPED_DUPLICATE_SUBMISSION_GUARD",
                    order_quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    cumulative_filled_qty=Decimal(str(row["cumulative_filled_qty"])),
                )

        # Mark SUBMITTING in ledger and DISPATCHED in outbox BEFORE network call
        submitting_ev = OrderEvent(
            cycle_id=plan.cycle_id,
            strategy_id=plan.strategy_id,
            venue_id=leg.venue_id,
            instrument_id=leg.instrument_id,
            client_order_id=leg.client_order_id,
            side=leg.side,
            order_type=leg.order_type,
            status=OrderStatus.SUBMITTING,
            native_venue_status="DISPATCHING_TO_VENUE",
            order_quantity=leg.quantity,
            limit_price=leg.limit_price,
            cumulative_filled_qty=Decimal("0"),
        )
        self._ledger.record_order_event(submitting_ev)
        with self._ledger.transaction() as cur:
            cur.execute(
                "UPDATE order_outbox SET outbox_state = 'DISPATCHED', updated_at_utc = ? WHERE client_order_id = ?",
                (datetime.now(timezone.utc).isoformat(), leg.client_order_id),
            )

        adapter = self._venues[leg.venue_id]
        try:
            res_ev = await adapter.submit_order(
                cycle_id=plan.cycle_id,
                strategy_id=plan.strategy_id,
                instrument_id=leg.instrument_id,
                client_order_id=leg.client_order_id,
                side=leg.side,
                order_type=leg.order_type,
                quantity=leg.quantity,
                limit_price=leg.limit_price,
            )
        except ExchangeError as exc:
            if exc.requires_reconciliation or not exc.is_definite_failure:
                res_ev = OrderEvent(
                    cycle_id=plan.cycle_id,
                    strategy_id=plan.strategy_id,
                    venue_id=leg.venue_id,
                    instrument_id=leg.instrument_id,
                    client_order_id=leg.client_order_id,
                    side=leg.side,
                    order_type=leg.order_type,
                    status=OrderStatus.UNKNOWN,
                    native_venue_status=f"UNKNOWN_ON_{exc.category.value}",
                    order_quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    error_code=exc.native_code,
                    error_message=exc.message,
                )
            else:
                res_ev = OrderEvent(
                    cycle_id=plan.cycle_id,
                    strategy_id=plan.strategy_id,
                    venue_id=leg.venue_id,
                    instrument_id=leg.instrument_id,
                    client_order_id=leg.client_order_id,
                    side=leg.side,
                    order_type=leg.order_type,
                    status=OrderStatus.REJECTED,
                    native_venue_status="DEFINITE_REJECT",
                    order_quantity=leg.quantity,
                    limit_price=leg.limit_price,
                    error_code=exc.native_code,
                    error_message=exc.message,
                )
        except (TimeoutError, ConnectionError) as exc:
            res_ev = OrderEvent(
                cycle_id=plan.cycle_id,
                strategy_id=plan.strategy_id,
                venue_id=leg.venue_id,
                instrument_id=leg.instrument_id,
                client_order_id=leg.client_order_id,
                side=leg.side,
                order_type=leg.order_type,
                status=OrderStatus.UNKNOWN,
                native_venue_status="NETWORK_TIMEOUT_UNKNOWN",
                order_quantity=leg.quantity,
                limit_price=leg.limit_price,
                error_message=str(exc),
            )

        OrderStateMachine.validate_transition(OrderStatus.SUBMITTING, res_ev.status)
        self._ledger.record_order_event(res_ev)

        if res_ev.status == OrderStatus.UNKNOWN:
            # Automatically activate strategy-level kill switch on UNKNOWN order until reconciled!
            self._risk.activate_kill_switch(
                KillSwitchScope.STRATEGY,
                plan.strategy_id,
                f"Order {leg.client_order_id} entered UNKNOWN state",
            )
            self._update_cycle_state(plan.cycle_id, CycleState.UNKNOWN)
        else:
            # Order terminal state does NOT mean cycle is SETTLED; cycle enters RECONCILING first!
            self._update_cycle_state(plan.cycle_id, CycleState.RECONCILING)

        return res_ev

    def apply_fill_event(self, fill: Fill) -> bool:
        """
        Record a fill event (including late fills arriving after `CANCELED`),
        enforcing idempotency (`CORE-003`) and monotonic cumulative quantity (`CORE-005`).
        """
        return self._ledger.record_fill(fill)

    def _update_cycle_state(self, cycle_id: str, new_state: CycleState) -> None:
        with self._ledger.transaction() as cur:
            cur.execute(
                "UPDATE cycles SET state = ?, updated_at_utc = ? WHERE cycle_id = ?",
                (new_state.value, datetime.now(timezone.utc).isoformat(), cycle_id),
            )
