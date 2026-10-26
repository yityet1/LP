"""Comprehensive Unit & Contract Tests for Sprint 3 (DATA-001, COST-001, STRAT-001, STRAT-002).
Covers:
- DATA-001: Partitioned Recorder (venue/date/symbol), missing/corrupted partition explicit error (zero silent fill), deterministic replay engine reproducibility.
- COST-001: Multi-level fill simulation (Bid/Ask direction, non-linear slippage across levels, insufficient depth, min_notional/step_size quantization, third-asset BNB fee discount, net PnL calculation).
- STRAT-001: Opportunity lifecycle state machine transitions, illegal transition rejection, TTL expiration, candidate deduplication, and LedgerStore persistence.
- STRAT-002 & S1-010: Route A Binance Spot Triangle Arbitrage Scanner (precomputed closed loops, incremental vs full scan equivalence, positive/zero/negative net PnL filtering, halted symbol rejection, and exact match against independent ReferenceCostCalculator).
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

from arb.config import RiskPolicyConfig, Settings
from arb.costs import (
    CostEngine,
    FeeSchedule,
    ReferenceCostCalculator,
)
from arb.domain import (
    Instrument,
    InstrumentType,
    OpportunityDecision,
    OpportunityStatus,
    OrderBook,
    OrderSide,
    PriceLevel,
    TradingStatus,
)
from arb.instruments import InstrumentRegistry
from arb.ledger import LedgerStore
from arb.marketdata import MarketHealthGate, OrderBookBuilder
from arb.replay import (
    DeterministicReplayEngine,
    MarketDataRecorder,
    RecordedMarketEvent,
    ReplayIntegrityError,
)
from arb.strategies.triangle import TriangleArbitrageScanner
from arb.strategy import (
    InvalidOpportunityTransitionError,
    OpportunityLifecycleManager,
    OpportunityRejectReason,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_YAML_PATH = REPO_ROOT / "configs" / "risk-policy.example.yaml"


def _create_spot_triangle_instruments(now: datetime) -> List[Instrument]:
    return [
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
            trading_status=TradingStatus.TRADING,
            event_time_utc=now,
            receive_time_utc=now,
        ),
        Instrument(
            venue_id="binance_spot",
            instrument_id="ETH-BTC",
            venue_symbol="ETHBTC",
            instrument_type=InstrumentType.SPOT,
            base_asset="ETH",
            quote_asset="BTC",
            settlement_asset="BTC",
            tick_size=Decimal("0.00001"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("0.0001"),
            min_qty=Decimal("0.001"),
            trading_status=TradingStatus.TRADING,
            event_time_utc=now,
            receive_time_utc=now,
        ),
        Instrument(
            venue_id="binance_spot",
            instrument_id="ETH-USDT",
            venue_symbol="ETHUSDT",
            instrument_type=InstrumentType.SPOT,
            base_asset="ETH",
            quote_asset="USDT",
            settlement_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("10.0"),
            min_qty=Decimal("0.001"),
            trading_status=TradingStatus.TRADING,
            event_time_utc=now,
            receive_time_utc=now,
        ),
    ]


class TestSprint3CostReplayAndTriangle(unittest.TestCase):
    # ==========================================================================
    # COST-001: Multi-Level Fill Simulation, Fees & Quantization Tests
    # ==========================================================================

    def test_cost001_multilevel_slippage_depth_and_third_asset_fees(self) -> None:
        now = datetime(2026, 10, 26, 3, 0, 0, tzinfo=timezone.utc)
        reg = InstrumentRegistry()
        btc_inst = _create_spot_triangle_instruments(now)[0]
        rules = reg.register(btc_inst)

        book = OrderBook(
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            bids=[
                PriceLevel(price=Decimal("50000.00"), quantity=Decimal("0.1000")),
                PriceLevel(price=Decimal("49900.00"), quantity=Decimal("0.2000")),
            ],
            asks=[
                PriceLevel(price=Decimal("50010.00"), quantity=Decimal("0.1000")),
                PriceLevel(price=Decimal("50050.00"), quantity=Decimal("0.3000")),
            ],
            last_update_id=100,
            event_time_utc=now,
            receive_time_utc=now,
        )

        # Configure BNB third-asset fee schedule (25% discount: 10 bps -> 7.5 bps)
        engine = CostEngine(
            fee_schedules={
                "binance_spot": FeeSchedule(
                    venue_id="binance_spot",
                    tier_name="VIP0_BNB",
                    maker_fee_rate=Decimal("0.0010"),
                    taker_fee_rate=Decimal("0.0010"),
                    fee_discount_asset="BNB",
                    fee_discount_rate=Decimal("0.75"),
                )
            }
        )

        # 1. Small BUY (0.05 BTC) fills completely at best ask (50010.00) with zero extra level slippage
        sim_small = engine.simulate_fill(book, rules, OrderSide.BUY, Decimal("0.05009"))  # floors to 0.0500
        self.assertTrue(sim_small.is_fully_executable)
        self.assertEqual(sim_small.quantized_base_qty, Decimal("0.0500"))
        self.assertEqual(sim_small.vwap_fill_price, Decimal("50010.00"))
        self.assertEqual(sim_small.slippage_bps, Decimal("0"))
        self.assertEqual(sim_small.levels_consumed, 1)

        # 2. Larger BUY (0.25 BTC) walks 2 levels: 0.1 @ 50010 + 0.15 @ 50050 -> non-linear slippage > 0
        sim_large = engine.simulate_fill(book, rules, OrderSide.BUY, Decimal("0.2500"))
        self.assertTrue(sim_large.is_fully_executable)
        self.assertEqual(sim_large.levels_consumed, 2)
        expected_vwap = (Decimal("0.1") * Decimal("50010") + Decimal("0.15") * Decimal("50050")) / Decimal("0.25")
        self.assertEqual(sim_large.vwap_fill_price, expected_vwap)
        self.assertTrue(sim_large.slippage_bps > sim_small.slippage_bps)

        # 3. Excessive BUY (0.50 BTC > total ask depth 0.40 BTC) rejected with INSUFFICIENT_DEPTH
        sim_over = engine.simulate_fill(book, rules, OrderSide.BUY, Decimal("0.5000"))
        self.assertFalse(sim_over.is_fully_executable)
        self.assertEqual(sim_over.reject_reason, "INSUFFICIENT_DEPTH")

        # 4. Tiny order below min_notional ($10) rejected with MIN_NOTIONAL_NOT_MET
        sim_tiny = engine.simulate_fill(book, rules, OrderSide.BUY, Decimal("0.0001"))  # 0.0001 * 50010 = $5.001 < $10
        self.assertFalse(sim_tiny.is_fully_executable)
        self.assertEqual(sim_tiny.reject_reason, "MIN_NOTIONAL_NOT_MET")

        # 5. Third-asset fee calculation (BNB @ $600)
        fee_est = engine.estimate_fees(
            venue_id="binance_spot",
            notional_in_settlement=Decimal("1200.00"),
            settlement_asset="USDT",
            is_maker=False,
            third_asset_price_in_settlement=Decimal("600.00"),
        )
        self.assertEqual(fee_est.fee_asset, "BNB")
        self.assertEqual(fee_est.effective_fee_rate, Decimal("0.00075"))
        self.assertEqual(fee_est.fee_in_settlement_asset, Decimal("0.9000000"))
        self.assertEqual(fee_est.fee_amount, Decimal("0.0015"))  # 0.90 USDT / 600 USDT/BNB

    # ==========================================================================
    # STRAT-001: Opportunity Lifecycle, Deduplication & Expiration Tests
    # ==========================================================================

    def test_strat001_opportunity_lifecycle_dedup_and_expiration(self) -> None:
        now = datetime(2026, 10, 26, 3, 10, 0, tzinfo=timezone.utc)
        store = LedgerStore(":memory:")
        lifecycle = OpportunityLifecycleManager(ledger=store)
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)
        reg = InstrumentRegistry()
        for inst in _create_spot_triangle_instruments(now):
            reg.register(inst)

        gate = MarketHealthGate(risk_policy=policy)
        cost_engine = CostEngine()
        scanner = TriangleArbitrageScanner(
            venue_id="binance_spot",
            settlement_asset="USDT",
            registry=reg,
            health_gate=gate,
            cost_engine=cost_engine,
            lifecycle=lifecycle,
            risk_policy=policy,
            initial_capital_quote=Decimal("1000.00"),
            candidate_ttl_ms=500,
        )
        cycles = scanner.build_graph(_create_spot_triangle_instruments(now))
        self.assertEqual(len(cycles), 2)  # USDT->BTC->ETH->USDT and USDT->ETH->BTC->USDT

        # Build profitable books for USDT -> BTC -> ETH -> USDT
        books = {
            "BTC-USDT": OrderBookBuilder("binance_spot", "BTC-USDT"),
            "ETH-BTC": OrderBookBuilder("binance_spot", "ETH-BTC"),
            "ETH-USDT": OrderBookBuilder("binance_spot", "ETH-USDT"),
        }
        books["BTC-USDT"].apply_snapshot(
            bids=[PriceLevel(price=Decimal("49990"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("50000"), quantity=Decimal("1.0"))],
            last_update_id=10,
            event_time_utc=now,
            receive_time_utc=now,
        )
        books["ETH-BTC"].apply_snapshot(
            bids=[PriceLevel(price=Decimal("0.0499"), quantity=Decimal("20.0"))],
            asks=[PriceLevel(price=Decimal("0.0500"), quantity=Decimal("20.0"))],
            last_update_id=20,
            event_time_utc=now,
            receive_time_utc=now,
        )
        books["ETH-USDT"].apply_snapshot(
            bids=[PriceLevel(price=Decimal("2550.00"), quantity=Decimal("20.0"))],
            asks=[PriceLevel(price=Decimal("2555.00"), quantity=Decimal("20.0"))],
            last_update_id=30,
            event_time_utc=now,
            receive_time_utc=now,
        )

        fwd_cycle = [c for c in cycles if c.intermediate_asset_1 == "BTC"][0]
        opp1 = scanner.evaluate_cycle(fwd_cycle, books, now_utc=now)
        self.assertEqual(opp1.status, OpportunityStatus.PAPER)
        self.assertEqual(opp1.decision, OpportunityDecision.PAPER)

        # Re-evaluating identical cycle on identical snapshot IDs MUST be rejected as DUPLICATE_OPPORTUNITY
        opp_dup = scanner.evaluate_cycle(fwd_cycle, books, now_utc=now)
        self.assertEqual(opp_dup.status, OpportunityStatus.REJECTED)
        self.assertIn(OpportunityRejectReason.DUPLICATE_OPPORTUNITY.value, opp_dup.reject_reason_codes)

        # Illegal state transition (e.g. from REJECTED back to PAPER) must raise InvalidOpportunityTransitionError
        with self.assertRaises(InvalidOpportunityTransitionError):
            lifecycle.transition(opp_dup, OpportunityStatus.PAPER, now_utc=now)

        # Expired candidate transition to EXECUTING transitions to EXPIRED instead
        expired_opp = lifecycle.transition(
            opp1,
            OpportunityStatus.EXECUTING,
            now_utc=now + timedelta(milliseconds=600),
        )
        self.assertEqual(expired_opp.status, OpportunityStatus.EXPIRED)
        self.assertEqual(expired_opp.decision, OpportunityDecision.EXPIRED)

    # ==========================================================================
    # STRAT-002 & S1-010: Route A Triangle Scanner & Reference Verification
    # ==========================================================================

    def test_strat002_and_s1_010_triangle_scanner_reference_match_and_incremental_scan(self) -> None:
        now = datetime(2026, 10, 26, 3, 20, 0, tzinfo=timezone.utc)
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)
        reg = InstrumentRegistry()
        insts = _create_spot_triangle_instruments(now)
        for inst in insts:
            reg.register(inst)

        gate = MarketHealthGate(risk_policy=policy)
        cost_engine = CostEngine()

        scanner_full = TriangleArbitrageScanner(
            venue_id="binance_spot",
            settlement_asset="USDT",
            registry=reg,
            health_gate=gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
            initial_capital_quote=Decimal("1000.00"),
        )
        scanner_full.build_graph(insts)

        books = {
            "BTC-USDT": OrderBookBuilder("binance_spot", "BTC-USDT"),
            "ETH-BTC": OrderBookBuilder("binance_spot", "ETH-BTC"),
            "ETH-USDT": OrderBookBuilder("binance_spot", "ETH-USDT"),
        }
        books["BTC-USDT"].apply_snapshot(
            bids=[PriceLevel(price=Decimal("49990.00"), quantity=Decimal("2.0"))],
            asks=[PriceLevel(price=Decimal("50000.00"), quantity=Decimal("2.0"))],
            last_update_id=101,
            event_time_utc=now,
            receive_time_utc=now,
        )
        books["ETH-BTC"].apply_snapshot(
            bids=[PriceLevel(price=Decimal("0.04990"), quantity=Decimal("50.0"))],
            asks=[PriceLevel(price=Decimal("0.05000"), quantity=Decimal("50.0"))],
            last_update_id=201,
            event_time_utc=now,
            receive_time_utc=now,
        )
        # Profitable ETH-USDT Bid = 2550.00 (theoretical parity = 2500.00, gross +2.0%, net ~ +1.69%)
        books["ETH-USDT"].apply_snapshot(
            bids=[PriceLevel(price=Decimal("2550.00"), quantity=Decimal("50.0"))],
            asks=[PriceLevel(price=Decimal("2555.00"), quantity=Decimal("50.0"))],
            last_update_id=301,
            event_time_utc=now,
            receive_time_utc=now,
        )

        exec_full, all_full = scanner_full.scan_all(books, now_utc=now)
        # Only forward cycle (USDT->BTC->ETH->USDT) is profitable; reverse cycle has negative net PnL and is rejected
        self.assertEqual(len(exec_full), 1)
        self.assertEqual(len(all_full), 2)
        winner = exec_full[0]

        # Compare against independent ReferenceCostCalculator (S1-010 P0 requirement)
        ref = ReferenceCostCalculator.compute_three_leg_cycle_reference(
            initial_usdt=Decimal("1000.00"),
            leg1_ask_px=Decimal("50000.00"),
            leg1_step=Decimal("0.0001"),
            leg2_ask_px=Decimal("0.05000"),
            leg2_step=Decimal("0.001"),
            leg3_bid_px=Decimal("2550.00"),
            leg3_step=Decimal("0.001"),
            taker_fee_rate=Decimal("0.0010"),
        )
        self.assertEqual(winner.legs[0].target_quantity, ref["qty_a"])
        self.assertEqual(winner.legs[1].target_quantity, ref["qty_b"])
        self.assertEqual(winner.legs[2].target_quantity, ref["qty_b_sell"])
        self.assertAlmostEqual(float(winner.expected_net_pnl), float(ref["net_pnl_usdt"]), places=6)
        self.assertAlmostEqual(float(winner.net_edge_bps), float(ref["net_edge_bps"]), places=4)

        # Verify incremental scan on ETH-USDT produces exact same executable candidate as full scan
        scanner_inc = TriangleArbitrageScanner(
            venue_id="binance_spot",
            settlement_asset="USDT",
            registry=reg,
            health_gate=gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
            initial_capital_quote=Decimal("1000.00"),
        )
        scanner_inc.build_graph(insts)
        exec_inc, all_inc = scanner_inc.scan_incremental("ETH-USDT", books, now_utc=now)
        self.assertEqual(len(exec_inc), 1)
        self.assertEqual(exec_inc[0].expected_net_pnl, winner.expected_net_pnl)
        self.assertEqual(exec_inc[0].net_edge_bps, winner.net_edge_bps)

        # Verify halted symbol immediately rejects affected triangles
        halted_eth_btc = insts[1].copy(update={"trading_status": TradingStatus.HALTED})
        reg.register(halted_eth_btc)
        exec_halted, all_halted = TriangleArbitrageScanner(
            venue_id="binance_spot",
            settlement_asset="USDT",
            registry=reg,
            health_gate=gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
        ).scan_all(books, now_utc=now)
        self.assertEqual(len(exec_halted), 0)

    # ==========================================================================
    # DATA-001: Partitioned Recorder & Deterministic Replay Engine Tests
    # ==========================================================================

    def test_data001_partitioned_recorder_and_deterministic_replay(self) -> None:
        t0 = datetime(2026, 10, 26, 4, 0, 0, tzinfo=timezone.utc)
        settings = Settings.from_env(RISK_YAML_PATH)
        cfg_hash = settings.compute_config_hash()

        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = MarketDataRecorder(tmpdir)

            # Record initial snapshots for all 3 triangle pairs + a diff update on ETH-USDT that creates an arbitrage opportunity
            events_to_record = [
                RecordedMarketEvent(
                    event_kind="snapshot",
                    venue_id="binance_spot",
                    instrument_id="BTC-USDT",
                    first_update_id=100,
                    final_update_id=100,
                    bids=[PriceLevel(price=Decimal("49990.00"), quantity=Decimal("2.0"))],
                    asks=[PriceLevel(price=Decimal("50000.00"), quantity=Decimal("2.0"))],
                    event_time_utc=t0,
                    receive_time_utc=t0,
                ),
                RecordedMarketEvent(
                    event_kind="snapshot",
                    venue_id="binance_spot",
                    instrument_id="ETH-BTC",
                    first_update_id=200,
                    final_update_id=200,
                    bids=[PriceLevel(price=Decimal("0.04990"), quantity=Decimal("50.0"))],
                    asks=[PriceLevel(price=Decimal("0.05000"), quantity=Decimal("50.0"))],
                    event_time_utc=t0 + timedelta(milliseconds=10),
                    receive_time_utc=t0 + timedelta(milliseconds=10),
                ),
                RecordedMarketEvent(
                    event_kind="snapshot",
                    venue_id="binance_spot",
                    instrument_id="ETH-USDT",
                    first_update_id=300,
                    final_update_id=300,
                    bids=[PriceLevel(price=Decimal("2500.00"), quantity=Decimal("50.0"))],
                    asks=[PriceLevel(price=Decimal("2505.00"), quantity=Decimal("50.0"))],
                    event_time_utc=t0 + timedelta(milliseconds=20),
                    receive_time_utc=t0 + timedelta(milliseconds=20),
                ),
                # Diff update spikes ETH-USDT Bid to 2555.00 & Ask to 2560.00 -> triggers profitable triangle!
                RecordedMarketEvent(
                    event_kind="diff",
                    venue_id="binance_spot",
                    instrument_id="ETH-USDT",
                    first_update_id=301,
                    final_update_id=301,
                    bids=[PriceLevel(price=Decimal("2555.00"), quantity=Decimal("25.0"))],
                    asks=[
                        PriceLevel(price=Decimal("2505.00"), quantity=Decimal("0")),
                        PriceLevel(price=Decimal("2560.00"), quantity=Decimal("50.0")),
                    ],
                    event_time_utc=t0 + timedelta(milliseconds=50),
                    receive_time_utc=t0 + timedelta(milliseconds=50),
                ),
            ]
            for ev in events_to_record:
                recorder.record_event(ev)

            req_parts = [
                ("binance_spot", "2026-10-26", "BTC-USDT"),
                ("binance_spot", "2026-10-26", "ETH-BTC"),
                ("binance_spot", "2026-10-26", "ETH-USDT"),
            ]
            loaded_events, manifest = recorder.load_dataset(req_parts)
            self.assertEqual(manifest.event_count, 4)
            self.assertTrue(manifest.dataset_version_id.startswith("ds_"))

            def run_single_replay() -> Tuple[str, int, Decimal]:
                reg = InstrumentRegistry()
                insts = _create_spot_triangle_instruments(t0)
                for i in insts:
                    reg.register(i)
                books_by_id = {
                    "BTC-USDT": OrderBookBuilder("binance_spot", "BTC-USDT"),
                    "ETH-BTC": OrderBookBuilder("binance_spot", "ETH-BTC"),
                    "ETH-USDT": OrderBookBuilder("binance_spot", "ETH-USDT"),
                }
                books_by_tuple = {("binance_spot", k): v for k, v in books_by_id.items()}
                scanner = TriangleArbitrageScanner(
                    venue_id="binance_spot",
                    settlement_asset="USDT",
                    registry=reg,
                    health_gate=MarketHealthGate(risk_policy=settings.risk_policy),
                    cost_engine=CostEngine(),
                    lifecycle=OpportunityLifecycleManager(),
                    risk_policy=settings.risk_policy,
                    initial_capital_quote=Decimal("1000.00"),
                )
                scanner.build_graph(insts)
                engine = DeterministicReplayEngine(config_version_hash=cfg_hash, order_books=books_by_tuple)
                res, opps = engine.run(
                    loaded_events,
                    manifest,
                    on_step_callback=lambda ev, clock_now: scanner.scan_incremental(
                        ev.instrument_id, books_by_id, now_utc=clock_now
                    )[1],
                )
                paper_opps = [o for o in opps if o.status == OpportunityStatus.PAPER]
                return res.result_fingerprint, res.executable_candidates_count, paper_opps[0].expected_net_pnl

            # Run replay twice: results MUST be 100% bit-for-bit identical
            fp1, exec_cnt1, pnl1 = run_single_replay()
            fp2, exec_cnt2, pnl2 = run_single_replay()
            self.assertEqual(fp1, fp2)
            self.assertEqual(exec_cnt1, 1)
            self.assertEqual(exec_cnt1, exec_cnt2)
            self.assertEqual(pnl1, pnl2)

            # Missing partition MUST raise ReplayIntegrityError explicitly (never silently skip)
            with self.assertRaisesRegex(ReplayIntegrityError, "Missing or empty market data partition"):
                recorder.load_partition("binance_spot", "2026-10-26", "SOL-USDT")

            # Corrupted record in partition MUST raise ReplayIntegrityError explicitly
            recorder.corrupt_partition_for_testing("binance_spot", "2026-10-26", "BTC-USDT", "{bad_json_line")
            with self.assertRaisesRegex(ReplayIntegrityError, "Corrupted market data record"):
                recorder.load_partition("binance_spot", "2026-10-26", "BTC-USDT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
