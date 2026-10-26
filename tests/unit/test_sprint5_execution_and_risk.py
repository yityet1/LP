import sys as _sys, pathlib as _pathlib
_src_dir = str(_pathlib.Path(__file__).resolve().parents[2] / "src")
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from arb.config.settings import Settings
from arb.costs.engine import CostEngine
from arb.domain.models import (
    BalanceSnapshot,
    CycleState,
    DataHealthStatus,
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
    InvalidOrderStateTransitionError,
    OrderStateMachine,
)
from arb.instruments.registry import InstrumentRegistry
from arb.ledger.store import LedgerStore, RiskDecisionRecord
from arb.marketdata.health import MarketHealth
from arb.marketdata.orderbook import SequenceStatus
from arb.paper.engine import (
    PaperExecutionSimulator,
    PaperFillBehavior,
    PaperMatchingConfig,
    PaperVenueAdapter,
)
from arb.reconciliation.engine import ReconciliationAction, ReconciliationEngine
from arb.risk.gateway import (
    AccountRiskState,
    KillSwitchScope,
    RiskGateway,
    RiskRejectCode,
)
from arb.testing.fault_injection import FaultInjectionHarness


def _make_plan(
    cycle_id: str,
    opportunity_id: str,
    config_hash: str,
    strategy_id: str = "strat_s1",
    notional_usdt: Decimal = Decimal("500"),
) -> ExecutionPlan:
    qty = (notional_usdt / Decimal("2500")).quantize(Decimal("0.0001"))
    return ExecutionPlan(
        cycle_id=cycle_id,
        opportunity_id=opportunity_id,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        execution_mode="paper",
        state=CycleState.RESERVED,
        legs=[
            ExecutionPlanLeg(
                client_order_id=f"{cycle_id}_leg1",
                venue_id="binance_spot",
                instrument_id="ETH-USDT",
                side=OrderSide.BUY,
                order_type=OrderType.IOC,
                quantity=qty,
                limit_price=Decimal("2500"),
                max_slippage_bps=Decimal("10"),
                rules_version="v1",
            ),
            ExecutionPlanLeg(
                client_order_id=f"{cycle_id}_leg2",
                venue_id="okx_spot",
                instrument_id="ETH-USDT",
                side=OrderSide.SELL,
                order_type=OrderType.IOC,
                quantity=qty,
                limit_price=Decimal("2510"),
                max_slippage_bps=Decimal("10"),
                rules_version="v1",
            ),
        ],
        reserved_asset="USDT",
        reserved_amount=notional_usdt,
        max_unhedged_notional=Decimal("500"),
        config_version_hash=config_hash,
    )


def _record_dummy_opportunity(ledger: LedgerStore, opportunity_id: str, strategy_id: str = "strat_s1") -> Opportunity:
    now = datetime.now(timezone.utc)
    opp = Opportunity(
        opportunity_id=opportunity_id,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        status=OpportunityStatus.PAPER,
        legs=[],
        input_snapshot_ids=["snap_1"],
        settlement_asset="USDT",
        gross_edge=Decimal("2.0"),
        fees=Decimal("1.0"),
        expected_net_pnl=Decimal("1.0"),
        net_edge_bps=Decimal("20.0"),
        capacity=Decimal("500.0"),
        expires_at=now + timedelta(seconds=5),
        decision=OpportunityDecision.PAPER,
    )
    ledger.record_opportunity(opp)
    return opp


class TestSprint5ExecutionAndRisk(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.settings = Settings.from_env(
            risk_policy_path=self.repo_root / "configs" / "risk-policy.example.yaml",
            env_override={"APP_ENV": "paper", "LIVE_TRADING_ENABLED": "false"},
        )

    def test_risk_001_pre_trade_gateway_and_concurrency_safe_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = LedgerStore(str(Path(tmpdir) / "s5_risk.sqlite"))
            cfg_hash = ledger.record_config_version(self.settings)
            gateway = RiskGateway(self.settings, ledger)

            now = datetime.now(timezone.utc)
            bal_bin = BalanceSnapshot(
                venue_id="binance_spot",
                account_id="acc_bin",
                asset="USDT",
                total_balance=Decimal("1000"),
                free_balance=Decimal("1000"),
                event_time_utc=now,
                receive_time_utc=now,
            )
            bal_okx = BalanceSnapshot(
                venue_id="okx_spot",
                account_id="acc_okx",
                asset="ETH",
                total_balance=Decimal("1.0"),
                free_balance=Decimal("1.0"),
                event_time_utc=now,
                receive_time_utc=now,
            )
            acct_state = AccountRiskState(
                balances={
                    ("binance_spot", "USDT"): bal_bin,
                    ("okx_spot", "ETH"): bal_okx,
                },
                strategy_active_notional_usdt={"strat_s1": Decimal("1000")},
                venue_active_notional_usdt={"binance_spot": Decimal("1000"), "okx_spot": Decimal("1000")},
                portfolio_active_notional_usdt=Decimal("2000"),
            )
            healthy_mh = [
                MarketHealth(
                    venue_id="binance_spot",
                    instrument_id="ETH-USDT",
                    policy_version=self.settings.risk_policy.policy_version,
                    status=DataHealthStatus.HEALTHY,
                    validity=True,
                    book_age_ms=25,
                    sequence_status=SequenceStatus.SYNCHRONIZED,
                    last_update_id=101,
                ),
                MarketHealth(
                    venue_id="okx_spot",
                    instrument_id="ETH-USDT",
                    policy_version=self.settings.risk_policy.policy_version,
                    status=DataHealthStatus.HEALTHY,
                    validity=True,
                    book_age_ms=30,
                    sequence_status=SequenceStatus.SYNCHRONIZED,
                    last_update_id=202,
                ),
            ]

            plan = _make_plan("cyc_risk_1", "opp_risk_1", cfg_hash, notional_usdt=Decimal("600"))
            decision = gateway.evaluate(plan, acct_state, healthy_mh, now_utc=now)
            self.assertTrue(decision.approved)
            self.assertIn(RiskRejectCode.PASS_ALL_GATES.value, decision.reason_codes)

            # Fail-closed when account_state is None (`CORE-008`)
            fail_closed = gateway.evaluate(plan, None, healthy_mh, now_utc=now)
            self.assertFalse(fail_closed.approved)
            self.assertIn(RiskRejectCode.RISK_SERVICE_UNAVAILABLE.value, fail_closed.reason_codes)

            # Concurrency-safe fund reservation: first 600 USDT out of 1000 USDT succeeds, second 600 USDT fails (`S1-011`)
            res1 = gateway.reserve_funds("cyc_risk_1", "strat_s1", "binance_spot", "USDT", Decimal("600"), bal_bin)
            self.assertEqual(res1.status, "ACTIVE")
            self.assertEqual(gateway.get_reserved_amount("binance_spot", "USDT"), Decimal("600"))

            with self.assertRaises(RuntimeError) as ctx:
                gateway.reserve_funds("cyc_risk_2", "strat_s2", "binance_spot", "USDT", Decimal("600"), bal_bin)
            self.assertIn(RiskRejectCode.INSUFFICIENT_FREE_BALANCE_FOR_RESERVATION.value, str(ctx.exception))

            # Release first reservation -> second reservation now succeeds
            gateway.release_reservation(res1.reservation_id, final_status="RELEASED")
            res2 = gateway.reserve_funds("cyc_risk_2", "strat_s2", "binance_spot", "USDT", Decimal("600"), bal_bin)
            self.assertEqual(res2.status, "ACTIVE")

            # Persistent kill switch across restart (`CORE-009`)
            gateway.activate_kill_switch(KillSwitchScope.PORTFOLIO, "GLOBAL", "MANUAL_DRILL_HALT")
            gateway_reloaded = RiskGateway(self.settings, ledger)
            decision_after_kill = gateway_reloaded.evaluate(plan, acct_state, healthy_mh, now_utc=now)
            self.assertFalse(decision_after_kill.approved)
            self.assertIn(RiskRejectCode.KILL_SWITCH_ACTIVE.value, decision_after_kill.reason_codes)

    def test_exec_001_order_fsm_outbox_lease_and_late_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = LedgerStore(str(Path(tmpdir) / "s5_exec.sqlite"))
            cfg_hash = ledger.record_config_version(self.settings)
            gateway = RiskGateway(self.settings, ledger)

            # Invalid transition rejected
            with self.assertRaises(InvalidOrderStateTransitionError):
                OrderStateMachine.validate_transition(OrderStatus.PREPARED, OrderStatus.FILLED)

            reg = InstrumentRegistry()
            now = datetime.now(timezone.utc)
            for vid, sym in [("binance_spot", "ETHUSDT"), ("okx_spot", "ETH-USDT")]:
                reg.register(
                    Instrument(
                        venue_id=vid,
                        instrument_id="ETH-USDT",
                        venue_symbol=sym,
                        instrument_type=InstrumentType.SPOT,
                        base_asset="ETH",
                        quote_asset="USDT",
                        settlement_asset="USDT",
                        tick_size=Decimal("0.01"),
                        step_size=Decimal("0.0001"),
                        min_notional=Decimal("10"),
                        min_qty=Decimal("0.0001"),
                        event_time_utc=now,
                        receive_time_utc=now,
                    )
                )
            cost_engine = CostEngine()
            paper_bin = PaperVenueAdapter(
                venue_id="binance_spot",
                registry=reg,
                cost_engine=cost_engine,
                order_books={},
                initial_balances={"USDT": Decimal("5000"), "ETH": Decimal("2.0")},
                default_config=PaperMatchingConfig(behavior=PaperFillBehavior.TIMEOUT_UNKNOWN),
            )
            orch_a = ExecutionOrchestrator(ledger, gateway, {"binance_spot": paper_bin}, worker_id="worker_A")
            orch_b = ExecutionOrchestrator(ledger, gateway, {"binance_spot": paper_bin}, worker_id="worker_B")

            _record_dummy_opportunity(ledger, "opp_exec_1")
            plan = _make_plan("cyc_exec_1", "opp_exec_1", cfg_hash)
            rd = RiskDecisionRecord(
                cycle_id=plan.cycle_id,
                opportunity_id=plan.opportunity_id,
                strategy_id=plan.strategy_id,
                policy_version=self.settings.risk_policy.policy_version,
                approved=True,
                reason_codes=["PASS"],
            )

            # Prepare cycle outbox with worker_A; competing worker_B must be blocked (`CORE-007`)
            outbox_recs = orch_a.prepare_cycle_outbox(plan, rd)
            self.assertEqual(len(outbox_recs), 2)
            with self.assertRaises(ConcurrentWorkerConflictError):
                orch_b.acquire_cycle_lease(plan.cycle_id)

            # Dispatch leg 1 -> times out into UNKNOWN and triggers strategy kill switch (`CORE-004`)
            ev1 = asyncio.run(orch_a.dispatch_leg(plan, plan.legs[0]))
            self.assertEqual(ev1.status, OrderStatus.UNKNOWN)
            self.assertTrue(ledger.is_kill_switch_active("STRATEGY:strat_s1"))

            # Re-dispatching same leg MUST skip duplicate submission (`CORE-004`)
            ev1_retry = asyncio.run(orch_a.dispatch_leg(plan, plan.legs[0]))
            self.assertEqual(ev1_retry.native_venue_status, "SKIPPED_DUPLICATE_SUBMISSION_GUARD")

            # Late fill after CANCELED updates cumulative filled qty monotonically (`CORE-005`)
            cancel_ev = asyncio.run(paper_bin.cancel_order("ETH-USDT", plan.legs[0].client_order_id))
            OrderStateMachine.validate_transition(OrderStatus.UNKNOWN, OrderStatus.CANCELED)
            ledger.record_order_event(cancel_ev)

            late_fill = Fill(
                fill_id="fill_late_001",
                venue_trade_id="trd_late_001",
                cycle_id=plan.cycle_id,
                strategy_id=plan.strategy_id,
                venue_id="binance_spot",
                instrument_id="ETH-USDT",
                client_order_id=plan.legs[0].client_order_id,
                venue_order_id=cancel_ev.venue_order_id or "v_ord_1",
                side=OrderSide.BUY,
                fill_qty=plan.legs[0].quantity,
                fill_price=Decimal("2500"),
                fee_amount=Decimal("0.25"),
                fee_asset="USDT",
                event_time_utc=now,
                receive_time_utc=now,
            )
            inserted = orch_a.apply_fill_event(late_fill)
            self.assertTrue(inserted)
            trail = ledger.get_cycle_audit_trail(plan.cycle_id)
            ord_row = [o for o in trail.orders if o["client_order_id"] == plan.legs[0].client_order_id][0]
            self.assertEqual(ord_row["status"], OrderStatus.FILLED.value)
            self.assertEqual(Decimal(ord_row["cumulative_filled_qty"]), plan.legs[0].quantity)

    def test_exec_002_reconciliation_engine_and_circuit_breakers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = LedgerStore(str(Path(tmpdir) / "s5_recon.sqlite"))
            cfg_hash = ledger.record_config_version(self.settings)
            gateway = RiskGateway(self.settings, ledger)
            reg = InstrumentRegistry()
            now = datetime.now(timezone.utc)
            reg.register(
                Instrument(
                    venue_id="binance_spot",
                    instrument_id="ETH-USDT",
                    venue_symbol="ETHUSDT",
                    instrument_type=InstrumentType.SPOT,
                    base_asset="ETH",
                    quote_asset="USDT",
                    settlement_asset="USDT",
                    tick_size=Decimal("0.01"),
                    step_size=Decimal("0.0001"),
                    min_notional=Decimal("10"),
                    min_qty=Decimal("0.0001"),
                    event_time_utc=now,
                    receive_time_utc=now,
                )
            )
            paper_bin = PaperVenueAdapter(
                venue_id="binance_spot",
                registry=reg,
                cost_engine=CostEngine(),
                order_books={},
                initial_balances={"USDT": Decimal("5000"), "ETH": Decimal("2.0")},
                default_config=PaperMatchingConfig(behavior=PaperFillBehavior.TIMEOUT_UNKNOWN),
            )
            orch = ExecutionOrchestrator(ledger, gateway, {"binance_spot": paper_bin})
            recon = ReconciliationEngine(
                ledger, gateway, {"binance_spot": paper_bin}, {"binance_spot": paper_bin}
            )

            # Single not-found tolerance on first query for nonexistent order
            rep_nf1 = asyncio.run(
                recon.reconcile_order_and_fills("cyc_nf", "strat_s1", "binance_spot", "ETH-USDT", "missing_cid_1")
            )
            self.assertFalse(rep_nf1.is_aligned)
            self.assertEqual(rep_nf1.action, ReconciliationAction.RETRY_LATER_NOT_FOUND_ONCE)

            # Second consecutive not-found triggers strategy halt & manual intervention
            rep_nf2 = asyncio.run(
                recon.reconcile_order_and_fills("cyc_nf", "strat_s1", "binance_spot", "ETH-USDT", "missing_cid_1")
            )
            self.assertFalse(rep_nf2.is_aligned)
            self.assertEqual(rep_nf2.action, ReconciliationAction.HALT_STRATEGY_MANUAL_INTERVENTION)
            self.assertTrue(ledger.is_kill_switch_active("STRATEGY:strat_s1"))

            # Reconcile UNKNOWN order that matched in background -> recovers fill & settles cycle
            _record_dummy_opportunity(ledger, "opp_recon_ok")
            plan = _make_plan("cyc_recon_ok", "opp_recon_ok", cfg_hash)
            plan.legs = [plan.legs[0]]
            rd = RiskDecisionRecord(
                cycle_id=plan.cycle_id,
                opportunity_id=plan.opportunity_id,
                strategy_id=plan.strategy_id,
                policy_version=self.settings.risk_policy.policy_version,
                approved=True,
                reason_codes=["PASS"],
            )
            orch.prepare_cycle_outbox(plan, rd)
            ev = asyncio.run(orch.dispatch_leg(plan, plan.legs[0]))
            self.assertEqual(ev.status, OrderStatus.UNKNOWN)

            rep_ok = asyncio.run(
                recon.reconcile_order_and_fills(
                    plan.cycle_id, plan.strategy_id, "binance_spot", "ETH-USDT", plan.legs[0].client_order_id
                )
            )
            self.assertTrue(rep_ok.is_aligned)
            self.assertEqual(rep_ok.recovered_fills_count, 1)
            self.assertFalse(ledger.is_kill_switch_active("STRATEGY:strat_s1"))

            # Balance mismatch triggers VENUE + STRATEGY kill switches (`CORE-010`)
            bal_rep = asyncio.run(
                recon.reconcile_balances(
                    venue_id="binance_spot",
                    strategy_id="strat_s1",
                    expected_balances={"USDT": Decimal("99999")},
                )
            )
            self.assertFalse(bal_rep.is_aligned)
            self.assertTrue(ledger.is_kill_switch_active("VENUE:binance_spot"))
            self.assertTrue(ledger.is_kill_switch_active("STRATEGY:strat_s1"))

    def test_exec_003_paper_trading_engine_both_legs_and_partial_leg_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = LedgerStore(str(Path(tmpdir) / "s5_paper.sqlite"))
            cfg_hash = ledger.record_config_version(self.settings)
            gateway = RiskGateway(self.settings, ledger)
            reg = InstrumentRegistry()
            now = datetime.now(timezone.utc)
            for vid, sym in [("binance_spot", "ETHUSDT"), ("okx_spot", "ETH-USDT")]:
                reg.register(
                    Instrument(
                        venue_id=vid,
                        instrument_id="ETH-USDT",
                        venue_symbol=sym,
                        instrument_type=InstrumentType.SPOT,
                        base_asset="ETH",
                        quote_asset="USDT",
                        settlement_asset="USDT",
                        tick_size=Decimal("0.01"),
                        step_size=Decimal("0.0001"),
                        min_notional=Decimal("10"),
                        min_qty=Decimal("0.0001"),
                        event_time_utc=now,
                        receive_time_utc=now,
                    )
                )
            cost_engine = CostEngine()
            paper_bin = PaperVenueAdapter(
                "binance_spot", reg, cost_engine, {}, {"USDT": Decimal("5000"), "ETH": Decimal("2.0")}
            )
            paper_okx = PaperVenueAdapter(
                "okx_spot", reg, cost_engine, {}, {"USDT": Decimal("5000"), "ETH": Decimal("2.0")}
            )
            venues = {"binance_spot": paper_bin, "okx_spot": paper_okx}
            orch = ExecutionOrchestrator(ledger, gateway, venues)
            sim = PaperExecutionSimulator(orch, ledger, venues)

            # Case 1: Both legs fill -> SETTLED with zero unhedged exposure
            _record_dummy_opportunity(ledger, "opp_paper_1")
            plan1 = _make_plan("cyc_paper_1", "opp_paper_1", cfg_hash)
            rd1 = RiskDecisionRecord(
                cycle_id=plan1.cycle_id,
                opportunity_id=plan1.opportunity_id,
                strategy_id=plan1.strategy_id,
                policy_version=self.settings.risk_policy.policy_version,
                approved=True,
                reason_codes=["PASS"],
            )
            orch.prepare_cycle_outbox(plan1, rd1)
            res1 = asyncio.run(sim.execute_plan(plan1))
            self.assertTrue(res1.is_paper_simulation)
            self.assertEqual(res1.cycle_state, CycleState.SETTLED)
            self.assertEqual(res1.unhedged_base_exposure, Decimal("0"))
            self.assertFalse(res1.recovery_triggered)

            # Case 2: Leg 1 fills, Leg 2 rejects -> real unhedged exposure & CycleState.RECOVERY (`S1-004`)
            paper_okx.set_instrument_behavior(
                "ETH-USDT", PaperMatchingConfig(behavior=PaperFillBehavior.REJECT_ORDER)
            )
            _record_dummy_opportunity(ledger, "opp_paper_2")
            plan2 = _make_plan("cyc_paper_2", "opp_paper_2", cfg_hash)
            rd2 = RiskDecisionRecord(
                cycle_id=plan2.cycle_id,
                opportunity_id=plan2.opportunity_id,
                strategy_id=plan2.strategy_id,
                policy_version=self.settings.risk_policy.policy_version,
                approved=True,
                reason_codes=["PASS"],
            )
            orch.prepare_cycle_outbox(plan2, rd2)
            res2 = asyncio.run(sim.execute_plan(plan2))
            self.assertEqual(res2.cycle_state, CycleState.RECOVERY)
            self.assertEqual(res2.unhedged_base_exposure, plan2.legs[0].quantity)
            self.assertTrue(res2.recovery_triggered)

    def test_exec_004_p0_fault_injection_drills_all_pass(self) -> None:
        reports = asyncio.run(FaultInjectionHarness.run_all_p0_drills(self.settings))
        self.assertEqual(len(reports), 4)
        for rep in reports:
            self.assertTrue(rep.passed, f"Drill {rep.scenario_id} failed: {rep.evidence}")


if __name__ == "__main__":
    unittest.main()
