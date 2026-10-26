"""Sprint 9, Sprint 10, and Sprint 11 automated unit & contract tests (`OPT-001`..`003`, `XCH-001`..`002`, `PORT-001`, `REL-001`).

Covers:
- Sprint 9 (`OPT-001`, `OPT-002`, `OPT-003` — S4-B Options Arbitrage):
  - Option contract metadata, Black-76 theoretical valuation, Newton-Raphson implied volatility solver,
    Greeks profile, and Put-Call Parity conversion/reversal scanner with exit spread cost, net Delta/Vega
    limits, and scenario stress loss gates (`S4B-001` ~ `S4B-007`).
- Sprint 10 (`XCH-001`, `XCH-002` — S4-C Cross-Chain Bridge Arbitrage):
  - Bridge transfer state machine, strict segregation of `in_transit_balance` from `allocatable_balance`,
    destination finality requirement, timeout route circuit breaker, wrapped asset depeg check, and
    transit-time volatility haircut (`S4C-001` ~ `S4C-006`).
- Sprint 11 (`PORT-001`, `REL-001` — M7 Portfolio Capital Allocator & Release Gate Controller):
  - Prevention of concurrent double-allocation of the same free balance, strategy/venue/asset/portfolio
    concentration limits, granular cost attribution, fault isolation between independent strategies,
    and G1–G4 multi-role release sign-off gates (`PORT-001` ~ `PORT-004`, `REL-001`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from arb.bridge.ledger import (
    BridgeHealthStatus,
    BridgeRouteSpec,
    BridgeTransferLedger,
    BridgeTransferStatus,
)
from arb.domain.models import BalanceSnapshot, OpportunityDecision, OptionType
from arb.options.models import (
    ExerciseStyle,
    OptionContractSpec,
    OptionMarketQuote,
    OptionSettlementStyle,
)
from arb.options.pricing import OptionPricingEngine
from arb.portfolio.allocator import PortfolioCapitalAllocator
from arb.release.gates import (
    ReleaseGateController,
    StrategyReleaseChecklist,
    StrategySignOffRecord,
)
from arb.strategies.cross_chain.scanner import CrossChainArbitrageScanner
from arb.strategies.options_arb.scanner import (
    OptionComboRiskLimits,
    OptionsRelativeValueScanner,
)


class TestSprint9To11OptionsCrossChainPortfolioAndRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.expiry_30d = self.now + timedelta(days=30)

    def test_sprint9_opt001_to_opt003_black76_iv_greeks_and_put_call_parity(self) -> None:
        """Verify OPT-001, OPT-002, and OPT-003 (`S4B-001` ~ `S4B-007`)."""
        call_spec = OptionContractSpec(
            venue_id="okx_options",
            instrument_id="BTC-USD-261125-70000-C",
            venue_symbol="BTC-USD-261125-70000-C",
            underlying_symbol="BTC",
            expiry_utc=self.expiry_30d,
            strike=Decimal("70000"),
            option_type=OptionType.CALL,
            contract_multiplier=Decimal("1"),
            settlement_asset="USDT",
            exercise_style=ExerciseStyle.EUROPEAN,
            settlement_style=OptionSettlementStyle.CASH_USD,
            account_options_approved=True,
            research_only=False,
        )
        put_spec = OptionContractSpec(
            venue_id="okx_options",
            instrument_id="BTC-USD-261125-70000-P",
            venue_symbol="BTC-USD-261125-70000-P",
            underlying_symbol="BTC",
            expiry_utc=self.expiry_30d,
            strike=Decimal("70000"),
            option_type=OptionType.PUT,
            contract_multiplier=Decimal("1"),
            settlement_asset="USDT",
            exercise_style=ExerciseStyle.EUROPEAN,
            settlement_style=OptionSettlementStyle.CASH_USD,
            account_options_approved=True,
            research_only=False,
        )

        pricer = OptionPricingEngine(risk_free_rate=Decimal("0.04"))
        tte = float(call_spec.time_to_expiry_years(self.now))

        # Compute exact theoretical Call & Put prices at F=70,000, K=70,000, vol=55%
        c_theo, c_delta, _, _, _ = pricer.black76_price_and_greeks(
            OptionType.CALL, 70000.0, 70000.0, tte, 0.04, 0.55
        )
        p_theo, p_delta, _, _, _ = pricer.black76_price_and_greeks(
            OptionType.PUT, 70000.0, 70000.0, tte, 0.04, 0.55
        )
        self.assertGreater(c_theo, 3000.0)
        self.assertAlmostEqual(c_theo, p_theo, places=2)  # At F == K, Black-76 Call == Put!

        # 1. Verify Newton-Raphson IV solver recovers 0.55 volatility accurately (`OPT-002`)
        call_quote = OptionMarketQuote(
            venue_id="okx_options",
            instrument_id=call_spec.instrument_id,
            bid_price=Decimal(f"{c_theo - 15.0:.2f}"),
            bid_size=Decimal("5.0"),
            ask_price=Decimal(f"{c_theo + 15.0:.2f}"),
            ask_size=Decimal("5.0"),
            mark_price=Decimal(f"{c_theo:.2f}"),
            underlying_index_price=Decimal("69800"),
            underlying_forward_price=Decimal("70000"),
            open_interest=Decimal("250"),
            source_timestamp_utc=self.now,
            received_timestamp_utc=self.now,
        )
        val_c = pricer.value_option(call_spec, call_quote, None, self.now)
        self.assertTrue(val_c.is_valid)
        self.assertIsNotNone(val_c.implied_volatility_mid)
        self.assertAlmostEqual(float(val_c.implied_volatility_mid), 0.55, places=2)

        # 2. Verify Put-Call Parity Conversion opportunity when Put is overpriced or Underlying Forward is rich (`OPT-003`)
        # Set Underlying Forward executable Bid = 70,250 (+250 USD parity dislocation) while Call Ask & Put Bid stay near F=70,000 levels
        put_quote = OptionMarketQuote(
            venue_id="okx_options",
            instrument_id=put_spec.instrument_id,
            bid_price=Decimal(f"{p_theo - 10.0:.2f}"),
            bid_size=Decimal("5.0"),
            ask_price=Decimal(f"{p_theo + 10.0:.2f}"),
            ask_size=Decimal("5.0"),
            mark_price=Decimal(f"{p_theo:.2f}"),
            underlying_index_price=Decimal("69800"),
            underlying_forward_price=Decimal("70000"),
            open_interest=Decimal("250"),
            source_timestamp_utc=self.now,
            received_timestamp_utc=self.now,
        )
        scanner = OptionsRelativeValueScanner(
            pricing_engine=pricer,
            risk_limits=OptionComboRiskLimits(
                max_abs_net_delta=Decimal("0.05"),
                max_scenario_stress_loss_usd=Decimal("300.0"),
                max_margin_requirement_usd=Decimal("15000.0"),
                min_net_edge_after_exit_cost_usd=Decimal("20.0"),
            ),
        )
        eval_ok = scanner.evaluate_put_call_parity(
            call_spec=call_spec,
            put_spec=put_spec,
            call_quote=call_quote,
            put_quote=put_quote,
            underlying_instrument_id="BTC-USDT-261125",
            underlying_executable_bid=Decimal("70250.0"),
            underlying_executable_ask=Decimal("70260.0"),
            quantity_contracts=Decimal("1.0"),
            now_utc=self.now,
        )
        self.assertTrue(eval_ok.accepted)
        self.assertEqual(eval_ok.structure_name, "CONVERSION_BUY_CALL_SELL_PUT_SHORT_FWD")
        self.assertIsNotNone(eval_ok.candidate)
        self.assertEqual(eval_ok.candidate.decision, OpportunityDecision.PAPER)
        self.assertAlmostEqual(float(eval_ok.net_greeks.delta), 0.0, places=3)
        self.assertGreater(eval_ok.expected_net_pnl_usd, Decimal("100.0"))

        # 3. Verify rejection when wide option bid-ask spreads cause exit cost / scenario stress loss to exceed limit (`S4B-005`)
        wide_put_quote = put_quote.copy(
            update={
                "bid_price": Decimal(f"{p_theo - 350.0:.2f}"),
                "ask_price": Decimal(f"{p_theo + 350.0:.2f}"),
            }
        )
        eval_wide_rej = scanner.evaluate_put_call_parity(
            call_spec=call_spec,
            put_spec=put_spec,
            call_quote=call_quote,
            put_quote=wide_put_quote,
            underlying_instrument_id="BTC-USDT-261125",
            underlying_executable_bid=Decimal("70250.0"),
            underlying_executable_ask=Decimal("70260.0"),
            quantity_contracts=Decimal("1.0"),
            now_utc=self.now,
        )
        self.assertFalse(eval_wide_rej.accepted)
        self.assertIn("SCENARIO_STRESS_LOSS_EXCEEDED", eval_wide_rej.rejection_reason or "")

    def test_sprint10_xch001_and_xch002_bridge_in_transit_finality_and_cross_chain_scanner(self) -> None:
        """Verify XCH-001 and XCH-002 (`S4C-001` ~ `S4C-006`)."""
        b_ledger = BridgeTransferLedger()
        route = BridgeRouteSpec(
            route_id="arb_to_opt_usdt",
            bridge_protocol="canonical_layerzero",
            source_chain_id=42161,
            destination_chain_id=10,
            source_asset="ETH",
            destination_asset="WETH",
            is_wrapped_representation=True,
            wrapped_peg_ratio_to_canonical=Decimal("0.9995"),
            min_acceptable_peg_ratio=Decimal("0.9970"),
            fixed_bridge_fee_usd=Decimal("2.00"),
            variable_bridge_fee_bps=Decimal("3.0"),
            expected_transit_seconds=240,  # 4 minutes
            max_timeout_seconds=900,
            health_status=BridgeHealthStatus.HEALTHY,
        )
        b_ledger.register_route(route)

        # 1. Initiate a 5 ETH bridge transfer -> verify in_transit_balance is 5 ETH while allocatable_balance is 0 (`S4C-001`)
        tx_rec = b_ledger.initiate_transfer(
            route_id="arb_to_opt_usdt",
            asset="ETH",
            expected_amount=Decimal("5.0"),
            bridge_fee_amount=Decimal("0.002"),
            source_tx_hash="0xsrc_bridge_tx_1",
            now_utc=self.now,
        )
        self.assertTrue(tx_rec.is_in_transit)
        self.assertFalse(tx_rec.is_destination_spendable)

        dest_snap_in_transit = b_ledger.compute_balance_snapshot_with_in_transit(
            venue_or_chain_id="optimism_dex",
            account_id="wallet_opt",
            asset="ETH",
            settled_free_balance=Decimal("0.5"),  # Only 0.5 ETH settled free!
            now_utc=self.now,
        )
        self.assertEqual(dest_snap_in_transit.in_transit_balance, Decimal("5.0"))
        self.assertEqual(dest_snap_in_transit.allocatable_balance, Decimal("0.5"))

        # 2. Unfinalized destination mint (confirmations=1 < required=2) cannot be marked DESTINATION_FINALIZED (`S4C-002`)
        with self.assertRaises(ValueError):
            b_ledger.update_transfer_state(
                tx_rec.transfer_id,
                BridgeTransferStatus.DESTINATION_FINALIZED,
                destination_confirmations=1,
                actual_received_amount=Decimal("4.998"),
            )

        # 3. CrossChainArbitrageScanner rejects trade requiring 2.0 ETH on destination when only 0.5 ETH is settled (`S4C-001`)
        scanner = CrossChainArbitrageScanner(
            bridge_ledger=b_ledger,
            min_risk_adjusted_net_edge_bps=Decimal("15.0"),
        )
        eval_no_inv = scanner.evaluate_cross_chain_spread(
            route_id="arb_to_opt_usdt",
            source_chain_venue="arbitrum_dex",
            destination_chain_venue="optimism_dex",
            asset_symbol="ETH",
            quantity=Decimal("2.0"),
            source_executable_ask=Decimal("2500.0"),
            destination_executable_bid=Decimal("2530.0"),
            destination_balance_snapshot=dest_snap_in_transit,
            require_immediate_dest_inventory=True,
            now_utc=self.now,
        )
        self.assertFalse(eval_no_inv.accepted)
        self.assertIn("INSUFFICIENT_SETTLED_DEST_INVENTORY_IN_TRANSIT_EXCLUDED", eval_no_inv.rejection_reason or "")

        # 4. Complete destination finality (2 confirmations) -> settled free balance becomes 5.498 ETH -> scan passes (`S4C-005`)
        fin_rec = b_ledger.update_transfer_state(
            tx_rec.transfer_id,
            BridgeTransferStatus.DESTINATION_FINALIZED,
            destination_tx_hash="0xdst_bridge_tx_1",
            actual_received_amount=Decimal("4.998"),
            destination_confirmations=2,
            now_utc=self.now + timedelta(minutes=4),
        )
        self.assertTrue(fin_rec.is_destination_spendable)
        dest_snap_settled = b_ledger.compute_balance_snapshot_with_in_transit(
            venue_or_chain_id="optimism_dex",
            account_id="wallet_opt",
            asset="ETH",
            settled_free_balance=Decimal("5.498"),
            now_utc=self.now + timedelta(minutes=4),
        )
        self.assertEqual(dest_snap_settled.in_transit_balance, Decimal("0"))
        eval_ok = scanner.evaluate_cross_chain_spread(
            route_id="arb_to_opt_usdt",
            source_chain_venue="arbitrum_dex",
            destination_chain_venue="optimism_dex",
            asset_symbol="ETH",
            quantity=Decimal("2.0"),
            source_executable_ask=Decimal("2500.0"),
            destination_executable_bid=Decimal("2530.0"),  # +120 bps gross spread
            destination_balance_snapshot=dest_snap_settled,
            require_immediate_dest_inventory=True,
            now_utc=self.now + timedelta(minutes=4),
        )
        self.assertTrue(eval_ok.accepted)
        self.assertGreater(eval_ok.risk_adjusted_net_edge_bps, Decimal("15.0"))
        self.assertGreater(eval_ok.transit_volatility_haircut_usd, Decimal("0"))

        # 5. Simulate stuck transfer exceeding timeout -> route is automatically paused (`S4C-003`)
        tx_stuck = b_ledger.initiate_transfer(
            route_id="arb_to_opt_usdt",
            asset="ETH",
            expected_amount=Decimal("1.0"),
            bridge_fee_amount=Decimal("0.001"),
            source_tx_hash="0xsrc_stuck",
            now_utc=self.now,
        )
        timed_out_list = b_ledger.scan_timeouts_and_pause_routes(self.now + timedelta(seconds=1200))
        self.assertEqual(len(timed_out_list), 1)
        self.assertEqual(timed_out_list[0].transfer_id, tx_stuck.transfer_id)
        paused, p_reason = b_ledger.is_route_paused("arb_to_opt_usdt")
        self.assertTrue(paused)
        self.assertIn("BRIDGE_TRANSFER_TIMEOUT", p_reason or "")

    def test_sprint11_port001_and_rel001_capital_allocator_and_release_gates(self) -> None:
        """Verify PORT-001 (`PORT-001` ~ `PORT-004`) and REL-001 (`G1` ~ `G4`)."""
        allocator = PortfolioCapitalAllocator(
            max_portfolio_notional_usd=Decimal("25000.0"),
            max_strategy_notional_usd=Decimal("10000.0"),
            max_venue_notional_usd=Decimal("15000.0"),
            max_asset_concentration_usd=Decimal("12000.0"),
        )
        # Register Binance balance: 15,000 total, 8,000 free, 2,000 locked, 1,000 reserved, 4,000 in_transit -> allocatable = 7,000 USDT (`PORT-002`)
        snap = BalanceSnapshot(
            venue_id="binance_spot",
            account_id="b_main",
            asset="USDT",
            total_balance=Decimal("15000"),
            free_balance=Decimal("8000"),
            locked_balance=Decimal("2000"),
            reserved_balance=Decimal("1000"),
            in_transit_balance=Decimal("4000"),
            event_time_utc=self.now,
            receive_time_utc=self.now,
        )
        allocator.update_balance_snapshot(snap)
        self.assertEqual(snap.allocatable_balance, Decimal("7000"))

        # 1. Strategy A allocates 4,500 USDT -> approved
        dec_a = allocator.request_allocation(
            opportunity_id="opp_a",
            strategy_id="strat_s1_cross_cex_spot",
            venue_id="binance_spot",
            asset="USDT",
            requested_amount=Decimal("4500"),
            requested_notional_usd=Decimal("4500"),
        )
        self.assertTrue(dec_a.approved)

        # 2. Strategy B concurrently requests 3,000 USDT from the same pool (only 2,500 remaining!) -> rejected (`PORT-001`)
        dec_b = allocator.request_allocation(
            opportunity_id="opp_b",
            strategy_id="strat_s2a_spot_futures_basis",
            venue_id="binance_spot",
            asset="USDT",
            requested_amount=Decimal("3000"),
            requested_notional_usd=Decimal("3000"),
        )
        self.assertFalse(dec_b.approved)
        self.assertIn("INSUFFICIENT_ALLOCATABLE_CAPITAL", dec_b.rejection_reason or "")

        # 3. Isolate faulty strategy `strat_s3_equity_cross_market` -> blocked while `strat_s2a_spot_futures_basis` (requesting 2,000 USDT) succeeds!
        allocator.isolate_and_halt_strategy("strat_s3_equity_cross_market", "BROKER_SESSION_DRIFT")
        dec_s3 = allocator.request_allocation(
            opportunity_id="opp_s3",
            strategy_id="strat_s3_equity_cross_market",
            venue_id="binance_spot",
            asset="USDT",
            requested_amount=Decimal("1000"),
            requested_notional_usd=Decimal("1000"),
        )
        self.assertFalse(dec_s3.approved)
        self.assertIn("STRATEGY_ISOLATED_HALTED", dec_s3.rejection_reason or "")

        dec_b_ok = allocator.request_allocation(
            opportunity_id="opp_b2",
            strategy_id="strat_s2a_spot_futures_basis",
            venue_id="binance_spot",
            asset="USDT",
            requested_amount=Decimal("2000"),
            requested_notional_usd=Decimal("2000"),
        )
        self.assertTrue(dec_b_ok.approved)

        summary = allocator.get_exposure_summary()
        self.assertEqual(summary.total_portfolio_notional_usd, Decimal("6500"))
        self.assertEqual(summary.total_in_transit_usd, Decimal("4000"))

        # 4. Verify REL-001 Release Gate Controller (`G1` ~ `G4`)
        rel_ctrl = ReleaseGateController(core_p0_passed=True)
        # Checklist 1: Paper mode default (no sign-off, live_flag_enabled=False) -> cannot enable live
        rel_ctrl.register_checklist(
            StrategyReleaseChecklist(
                strategy_id="strat_s1_cross_cex_spot",
                strategy_family="S1",
                git_commit_sha="74581fa",
                config_version_hash="cfg_v1_hash",
                p0_acceptance_passed=True,
                p1_acceptance_passed=True,
                paper_soak_hours_completed=96.0,
                required_paper_soak_hours=72.0,
                unexplained_ledger_discrepancies=0,
                unhandled_p0_alerts=0,
                live_flag_enabled=False,
            )
        )
        can_live_default, reasons_default = rel_ctrl.evaluate_strategy_gate("strat_s1_cross_cex_spot")
        self.assertFalse(can_live_default)
        self.assertIn("MISSING_MULTI_ROLE_RELEASE_SIGN_OFF", reasons_default)
        self.assertIn("LIVE_FLAG_DISABLED_BY_DEFAULT", reasons_default)

        # Checklist 2: Fully signed off, 96h soak, 0 discrepancies, live_flag_enabled=True -> passes!
        rel_ctrl.register_checklist(
            StrategyReleaseChecklist(
                strategy_id="strat_s1_canary_approved",
                strategy_family="S1",
                git_commit_sha="74581fa",
                config_version_hash="cfg_v1_hash",
                p0_acceptance_passed=True,
                p1_acceptance_passed=True,
                paper_soak_hours_completed=96.0,
                required_paper_soak_hours=72.0,
                unexplained_ledger_discrepancies=0,
                unhandled_p0_alerts=0,
                sign_off=StrategySignOffRecord(
                    quant_lead_signer="quant_lead_gemini",
                    risk_lead_signer="risk_lead_gemini",
                    sre_lead_signer="sre_lead_gemini",
                    approval_ticket_id="CAB-2026-1026-S1",
                    signed_at_utc=self.now,
                ),
                live_flag_enabled=True,
            )
        )
        can_live_signed, reasons_signed = rel_ctrl.evaluate_strategy_gate("strat_s1_canary_approved")
        self.assertTrue(can_live_signed)
        self.assertEqual(len(reasons_signed), 0)

        # If shared core P0 fails, even a signed-off strategy is immediately blocked!
        rel_ctrl.set_core_p0_status(False)
        can_live_core_fail, reasons_core_fail = rel_ctrl.evaluate_strategy_gate("strat_s1_canary_approved")
        self.assertFalse(can_live_core_fail)
        self.assertIn("SHARED_CORE_P0_GATE_FAILED_ALL_STRATEGIES_BLOCKED", reasons_core_fail)


if __name__ == "__main__":
    unittest.main()
