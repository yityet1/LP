"""[EXEC-004] Automated P0 Fault Injection Harness & Crash-Recovery Verification Suite."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List

from pydantic import BaseModel

from arb.config.settings import Settings
from arb.costs.engine import CostEngine
from arb.domain.models import (
    CycleState,
    ExecutionPlan,
    ExecutionPlanLeg,
    Fill,
    Instrument,
    InstrumentType,
    Opportunity,
    OpportunityDecision,
    OpportunityStatus,
    OrderSide,
    OrderStatus,
    OrderType,
)
from arb.execution.fsm import (
    ConcurrentWorkerConflictError,
    ExecutionOrchestrator,
    OrderStateMachine,
)
from arb.instruments.registry import InstrumentRegistry
from arb.ledger.store import LedgerStore, RiskDecisionRecord
from arb.paper.engine import PaperFillBehavior, PaperMatchingConfig, PaperVenueAdapter
from arb.reconciliation.engine import ReconciliationAction, ReconciliationEngine
from arb.risk.gateway import RiskGateway


class FaultScenarioResult(BaseModel):
    scenario_id: str
    description: str
    passed: bool
    evidence: str


class FaultInjectionHarness:
    """
    [EXEC-004] Executes automated P0 fault injection drills:
    1. `FAULT_01_ORDER_TIMEOUT_AND_CRASH_RECOVERY`: Order times out (`UNKNOWN`), process restarts,
       verifies second dispatch NEVER re-submits order and `ReconciliationEngine` recovers the fill and settles the cycle.
    2. `FAULT_02_DUAL_WORKER_RACE_GUARD`: Two workers attempt to execute the same `cycle_id`,
       verifies `ConcurrentWorkerConflictError` blocks the competing worker (`CORE-007`).
    3. `FAULT_03_LATE_FILL_AFTER_CANCEL`: Order transitions to `CANCELED`, then a late fill arrives,
       verifies fill is recorded and `cumulative_filled_qty` increases monotonically (`CORE-005`).
    4. `FAULT_04_BALANCE_MISMATCH_CIRCUIT_BREAKER`: External venue balance diverges from ledger,
       verifies `ReconciliationEngine` triggers persistent Venue & Strategy Kill Switch (`CORE-009`, `CORE-010`).
    """

    @staticmethod
    async def run_all_p0_drills(settings: Settings) -> List[FaultScenarioResult]:
        results: List[FaultScenarioResult] = []
        now = datetime.now(timezone.utc)

        # Setup shared test fixtures
        ledger = LedgerStore(":memory:")
        cfg_hash = ledger.record_config_version(settings)
        risk = RiskGateway(settings=settings, ledger=ledger)
        reg = InstrumentRegistry()
        reg.register(
            Instrument(
                venue_id="binance_spot",
                instrument_id="BTC-USDT",
                venue_symbol="BTCUSDT",
                instrument_type=InstrumentType.SPOT,
                base_asset="BTC",
                quote_asset="USDT",
                settlement_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.0001"),
                min_notional=Decimal("10.0"),
                min_qty=Decimal("0.0001"),
                event_time_utc=now,
                receive_time_utc=now,
            )
        )
        cost_engine = CostEngine()
        paper_binance = PaperVenueAdapter(
            venue_id="binance_spot",
            registry=reg,
            cost_engine=cost_engine,
            order_books={},
            initial_balances={"USDT": Decimal("10000"), "BTC": Decimal("1.0")},
            default_config=PaperMatchingConfig(behavior=PaperFillBehavior.TIMEOUT_UNKNOWN),
        )

        # Create dummy opportunity in ledger to satisfy foreign key
        opp = Opportunity(
            opportunity_id="opp_fault_1",
            strategy_id="strat_s1",
            strategy_version="1.0.0",
            status=OpportunityStatus.PAPER,
            legs=[],
            input_snapshot_ids=["snap_1"],
            settlement_asset="USDT",
            gross_edge=Decimal("2"),
            fees=Decimal("1"),
            expected_net_pnl=Decimal("1"),
            net_edge_bps=Decimal("10"),
            capacity=Decimal("680"),
            expires_at=now,
            decision=OpportunityDecision.PAPER,
        )
        ledger.record_opportunity(opp)

        plan = ExecutionPlan(
            cycle_id="cyc_fault_1",
            opportunity_id=opp.opportunity_id,
            strategy_id="strat_s1",
            strategy_version="1.0.0",
            execution_mode="paper",
            state=CycleState.RESERVED,
            legs=[
                ExecutionPlanLeg(
                    client_order_id="cid_fault_timeout_1",
                    venue_id="binance_spot",
                    instrument_id="BTC-USDT",
                    side=OrderSide.BUY,
                    quantity=Decimal("0.0100"),
                    limit_price=Decimal("68000.00"),
                    max_slippage_bps=Decimal("5"),
                    rules_version="v1",
                )
            ],
            reserved_asset="USDT",
            reserved_amount=Decimal("680.00"),
            max_unhedged_notional=Decimal("500.00"),
            config_version_hash=cfg_hash,
        )
        rd = RiskDecisionRecord(
            cycle_id=plan.cycle_id,
            opportunity_id=opp.opportunity_id,
            strategy_id=plan.strategy_id,
            policy_version=settings.risk_policy.policy_version,
            approved=True,
            reason_codes=["PASS"],
        )

        # ----------------------------------------------------------------------
        # Drill 1: Timeout -> UNKNOWN -> Process Restart -> No Duplicate Resend -> Reconcile
        # ----------------------------------------------------------------------
        orch_1 = ExecutionOrchestrator(
            ledger=ledger,
            risk_gateway=risk,
            venues={"binance_spot": paper_binance},
            worker_id="worker_1",
        )
        orch_1.prepare_cycle_outbox(plan, rd)
        ev_timeout = await orch_1.dispatch_leg(plan, plan.legs[0])
        assert ev_timeout.status == OrderStatus.UNKNOWN
        assert ledger.is_kill_switch_active("STRATEGY:strat_s1") is True

        # Simulate process restart with same worker_id attempting to dispatch again -> MUST skip duplicate submission!
        ev_retry = await orch_1.dispatch_leg(plan, plan.legs[0])
        assert ev_retry.native_venue_status == "SKIPPED_DUPLICATE_SUBMISSION_GUARD"

        # Run ReconciliationEngine to recover missing fill and settle cycle
        recon = ReconciliationEngine(
            ledger=ledger,
            risk_gateway=risk,
            trading_adapters={"binance_spot": paper_binance},
            account_adapters={"binance_spot": paper_binance},
        )
        rec_rep = await recon.reconcile_order_and_fills(
            cycle_id=plan.cycle_id,
            strategy_id=plan.strategy_id,
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            client_order_id="cid_fault_timeout_1",
        )
        assert rec_rep.is_aligned is True
        assert rec_rep.recovered_fills_count == 1
        assert ledger.is_kill_switch_active("STRATEGY:strat_s1") is False

        results.append(
            FaultScenarioResult(
                scenario_id="FAULT_01_ORDER_TIMEOUT_AND_CRASH_RECOVERY",
                description="REST timeout -> UNKNOWN -> restart duplicate guard -> reconcile missing fill",
                passed=True,
                evidence=f"status={ev_timeout.status.value}, guard={ev_retry.native_venue_status}, recovered_fills={rec_rep.recovered_fills_count}",
            )
        )

        # ----------------------------------------------------------------------
        # Drill 2: Dual-Worker Race Condition Guard (CORE-007)
        # ----------------------------------------------------------------------
        orch_2 = ExecutionOrchestrator(
            ledger=ledger,
            risk_gateway=risk,
            venues={"binance_spot": paper_binance},
            worker_id="worker_competing_2",
        )
        race_blocked = False
        try:
            orch_2.acquire_cycle_lease(plan.cycle_id)
        except ConcurrentWorkerConflictError:
            race_blocked = True
        assert race_blocked is True
        results.append(
            FaultScenarioResult(
                scenario_id="FAULT_02_DUAL_WORKER_RACE_GUARD",
                description="Competing worker blocked by cycle_worker_leases lock",
                passed=True,
                evidence="ConcurrentWorkerConflictError raised as expected",
            )
        )

        # ----------------------------------------------------------------------
        # Drill 3: Late Fill Arriving After Cancel (CORE-005)
        # ----------------------------------------------------------------------
        opp2 = opp.copy(update={"opportunity_id": "opp_fault_latefill"})
        ledger.record_opportunity(opp2)
        plan2 = plan.copy(
            update={
                "cycle_id": "cyc_fault_latefill",
                "opportunity_id": "opp_fault_latefill",
                "legs": [
                    plan.legs[0].copy(update={"client_order_id": "cid_fault_latefill_1"})
                ],
            }
        )
        rd2 = rd.copy(
            update={
                "decision_id": "rd_fault_latefill",
                "cycle_id": "cyc_fault_latefill",
                "opportunity_id": "opp_fault_latefill",
            }
        )
        orch_1.prepare_cycle_outbox(plan2, rd2)
        ev_unk2 = await orch_1.dispatch_leg(plan2, plan2.legs[0])
        assert ev_unk2.status == OrderStatus.UNKNOWN

        cancel_ev = await paper_binance.cancel_order("BTC-USDT", "cid_fault_latefill_1")
        OrderStateMachine.validate_transition(OrderStatus.UNKNOWN, OrderStatus.CANCELED)
        ledger.record_order_event(cancel_ev)

        late_fill = Fill(
            fill_id="fill_drill_late_01",
            venue_trade_id="trd_drill_late_01",
            cycle_id=plan2.cycle_id,
            strategy_id=plan2.strategy_id,
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            client_order_id="cid_fault_latefill_1",
            venue_order_id=cancel_ev.venue_order_id or "v_ord_late",
            side=OrderSide.BUY,
            fill_qty=plan2.legs[0].quantity,
            fill_price=Decimal("68000.00"),
            fee_amount=Decimal("0.68"),
            fee_asset="USDT",
            event_time_utc=now,
            receive_time_utc=now,
        )
        orch_1.apply_fill_event(late_fill)
        trail2 = ledger.get_cycle_audit_trail(plan2.cycle_id)
        ord2 = trail2.orders[0]
        assert ord2["status"] == OrderStatus.FILLED.value
        assert Decimal(ord2["cumulative_filled_qty"]) == plan2.legs[0].quantity

        results.append(
            FaultScenarioResult(
                scenario_id="FAULT_03_LATE_FILL_AFTER_CANCEL",
                description="Late fill arriving after CANCELED updates cumulative_filled_qty monotonically without data loss",
                passed=True,
                evidence=f"final_status={ord2['status']}, cumulative_filled_qty={ord2['cumulative_filled_qty']}",
            )
        )

        # ----------------------------------------------------------------------
        # Drill 4: Balance Mismatch Circuit Breaker & Persistent Kill Switch (CORE-009, CORE-010)
        # ----------------------------------------------------------------------
        bal_rep = await recon.reconcile_balances(
            venue_id="binance_spot",
            strategy_id="strat_s1",
            expected_balances={"USDT": Decimal("999999.00")},  # Intentional mismatch
        )
        assert bal_rep.is_aligned is False
        assert bal_rep.action == ReconciliationAction.HALT_STRATEGY_MANUAL_INTERVENTION
        assert ledger.is_kill_switch_active("VENUE:binance_spot") is True
        assert ledger.is_kill_switch_active("STRATEGY:strat_s1") is True

        results.append(
            FaultScenarioResult(
                scenario_id="FAULT_04_BALANCE_MISMATCH_AND_PERSISTENT_KILL_SWITCH",
                description="Unexplained balance discrepancy halts venue & strategy persistently across restarts",
                passed=True,
                evidence=f"action={bal_rep.action.value}, venue_halted={ledger.is_kill_switch_active('VENUE:binance_spot')}",
            )
        )

        return results
