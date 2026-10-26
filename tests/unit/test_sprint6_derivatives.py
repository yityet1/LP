"""[DERIV-004] Sprint 6 Unit, Contract & S2 Acceptance Matrix Tests (`DERIV-001` ~ `DERIV-004`, `S2-001` ~ `S2-012`)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import unittest

from arb.config.settings import RiskPolicyConfig, Settings
from arb.costs.engine import CostEngine, FeeSchedule
from arb.domain.models import (
    CycleState,
    ExecutionPlan,
    ExecutionPlanLeg,
    Instrument,
    InstrumentType,
    OpportunityDecision,
    OpportunityStatus,
    OrderSide,
    OrderType,
    PositionSnapshot,
    PriceLevel,
)
from arb.execution.fsm import ExecutionOrchestrator
from arb.instruments.registry import InstrumentRegistry
from arb.ledger.store import LedgerStore, RiskDecisionRecord
from arb.marketdata.health import MarketHealthGate
from arb.marketdata.orderbook import OrderBookBuilder
from arb.paper.engine import PaperExecutionSimulator, PaperVenueAdapter
from arb.risk.gateway import RiskGateway
from arb.strategies.basis.scanner import BasisArbitrageScanner, BasisPairSpec
from arb.strategies.funding.scanner import (
    FundingCarryPairSpec,
    FundingRateArbitrageScanner,
    MarginAndLiquidationMonitor,
    MarginSafetyAction,
)
from arb.strategy.lifecycle import OpportunityLifecycleManager
from arb.venues.binance_spot.rest import HttpResponsePayload, HttpTransport
from arb.venues.derivatives.adapters import (
    BinanceUsdmDerivativesAdapter,
    DerivativeFundingSnapshot,
    OkxSwapDerivativesAdapter,
)


class _StubDerivTransport(HttpTransport):
    def __init__(self, handler) -> None:
        self._handler = handler

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 3000,
    ) -> HttpResponsePayload:
        return self._handler(method, url, params)


class TestSprint6DerivativesAndS2Matrix(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path("/working_dir/c_8b6fc0dea8cfd9c3/arb-platform")
        self.settings = Settings.from_env(
            risk_policy_path=self.repo_root / "configs" / "risk-policy.example.yaml",
            env_override={"APP_ENV": "paper", "LIVE_TRADING_ENABLED": "false"},
        )
        self.policy: RiskPolicyConfig = self.settings.risk_policy

    def test_deriv_001_binance_usdm_and_okx_swap_adapters_and_ctval_multiplier(self) -> None:
        now = datetime(2026, 10, 26, 5, 0, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        next_funding_ms = int((now + timedelta(hours=8)).timestamp() * 1000)

        def binance_router(method: str, url: str, params: Optional[Dict[str, Any]]) -> HttpResponsePayload:
            if url.endswith("/fapi/v1/exchangeInfo"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "symbols": [
                            {
                                "symbol": "BTCUSDT",
                                "contractType": "PERPETUAL",
                                "baseAsset": "BTC",
                                "quoteAsset": "USDT",
                                "marginAsset": "USDT",
                                "status": "TRADING",
                                "fundingIntervalHours": 8,
                                "filters": [
                                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                                ],
                            }
                        ]
                    },
                )
            if url.endswith("/fapi/v1/depth"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "lastUpdateId": 7001,
                        "T": now_ms,
                        "bids": [["68100.0", "2.0"]],
                        "asks": [["68105.0", "2.0"]],
                    },
                )
            if url.endswith("/fapi/v1/premiumIndex"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "symbol": "BTCUSDT",
                        "markPrice": "68120.50",
                        "indexPrice": "68010.00",
                        "lastFundingRate": "0.00025000",
                        "nextFundingTime": next_funding_ms,
                        "time": now_ms,
                    },
                )
            if url.endswith("/fapi/v2/positionRisk"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body=[
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "-0.010",
                            "entryPrice": "68100.0",
                            "markPrice": "68120.5",
                            "unRealizedProfit": "-0.205",
                            "liquidationPrice": "79500.0",
                            "isolatedMargin": "150.0",
                            "positionSide": "BOTH",
                        }
                    ],
                )
            raise AssertionError(f"Unexpected URL {url}")

        b_adapter = BinanceUsdmDerivativesAdapter(_StubDerivTransport(binance_router))
        b_insts = asyncio.run(b_adapter.fetch_instruments())
        self.assertEqual(len(b_insts), 1)
        self.assertEqual(b_insts[0].instrument_id, "BTC-USDT-PERP")
        self.assertEqual(b_insts[0].instrument_type, InstrumentType.PERPETUAL)

        b_snap = asyncio.run(b_adapter.fetch_market_snapshot("BTC-USDT-PERP"))
        # Verify S2-001: mark_price (68120.50) and index_price (68010.00) are separated from executable bid (68100.0) & ask (68105.0)
        self.assertEqual(b_snap.mark_price, Decimal("68120.50"))
        self.assertEqual(b_snap.index_price, Decimal("68010.00"))
        self.assertEqual(b_snap.get_executable_price(OrderSide.SELL), Decimal("68100.0"))
        self.assertEqual(b_snap.get_executable_price(OrderSide.BUY), Decimal("68105.0"))

        b_pos = asyncio.run(b_adapter.fetch_positions())
        self.assertEqual(len(b_pos), 1)
        self.assertEqual(b_pos[0].quantity, Decimal("-0.010"))
        self.assertEqual(b_pos[0].liquidation_price, Decimal("79500.0"))

        # Test OKX Swap adapter with ctVal = 0.01 BTC per contract (`S2-003`)
        def okx_router(method: str, url: str, params: Optional[Dict[str, Any]]) -> HttpResponsePayload:
            if url.endswith("/api/v5/public/instruments"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "settleCcy": "USDT",
                                "ctVal": "0.01",
                                "tickSz": "0.1",
                                "lotSz": "1",
                                "minSz": "1",
                                "state": "live",
                            }
                        ],
                    },
                )
            if url.endswith("/api/v5/account/positions"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "posSide": "net",
                                "pos": "-2",
                                "ctVal": "0.01",
                                "avgPx": "68200.0",
                                "markPx": "68150.0",
                                "upl": "1.0",
                                "imr": "200.0",
                                "liqPx": "78000.0",
                            }
                        ],
                    },
                )
            raise AssertionError(f"Unexpected OKX URL {url}")

        o_adapter = OkxSwapDerivativesAdapter(_StubDerivTransport(okx_router))
        o_insts = asyncio.run(o_adapter.fetch_instruments())
        self.assertEqual(o_insts[0].contract_multiplier, Decimal("0.01"))
        self.assertEqual(o_insts[0].step_size, Decimal("1"))
        o_pos = asyncio.run(o_adapter.fetch_positions())
        self.assertEqual(o_pos[0].quantity, Decimal("-2"))
        self.assertEqual(o_pos[0].contract_multiplier, Decimal("0.01"))

    def test_deriv_002_s2a_spot_futures_basis_engine_and_multiplier_alignment(self) -> None:
        now = datetime(2026, 10, 26, 5, 10, 0, tzinfo=timezone.utc)
        reg = InstrumentRegistry()

        # Register Binance Spot BTC-USDT (multiplier=1, step=0.0001) and OKX Dated Future BTC-USDT-261225 (multiplier=0.01 BTC/contract, step=1 contract)
        reg.register(
            Instrument(
                venue_id="binance_spot",
                instrument_id="BTC-USDT",
                venue_symbol="BTCUSDT",
                instrument_type=InstrumentType.SPOT,
                base_asset="BTC",
                quote_asset="USDT",
                settlement_asset="USDT",
                contract_multiplier=Decimal("1"),
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.0001"),
                min_notional=Decimal("10"),
                min_qty=Decimal("0.0001"),
                event_time_utc=now,
                receive_time_utc=now,
            )
        )
        reg.register(
            Instrument(
                venue_id="okx_swap",
                instrument_id="BTC-USDT-261225",
                venue_symbol="BTC-USDT-261225",
                instrument_type=InstrumentType.DATED_FUTURE,
                base_asset="BTC",
                quote_asset="USDT",
                settlement_asset="USDT",
                contract_multiplier=Decimal("0.01"),  # 1 contract = 0.01 BTC (`S2-003`)
                tick_size=Decimal("0.1"),
                step_size=Decimal("1"),
                min_notional=Decimal("10"),
                min_qty=Decimal("1"),
                expiry=now + timedelta(days=30),
                event_time_utc=now,
                receive_time_utc=now,
            )
        )

        spot_book = OrderBookBuilder("binance_spot", "BTC-USDT")
        spot_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("67990"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0"))],
            last_update_id=101,
            event_time_utc=now,
            receive_time_utc=now,
        )
        # Note: Deriv BID is 68,680 (+100 bps gross basis over 68,000 spot ask), while mark_price is artificially set to 70,000
        # to prove scanner uses Deriv BID (68,680) and NEVER mark_price (70,000) (`S2-001`)!
        deriv_book = OrderBookBuilder("okx_swap", "BTC-USDT-261225")
        deriv_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68680"), quantity=Decimal("100"))],
            asks=[PriceLevel(price=Decimal("68690"), quantity=Decimal("100"))],
            last_update_id=202,
            event_time_utc=now,
            receive_time_utc=now,
        )
        books: Dict[Tuple[str, str], OrderBookBuilder] = {
            ("binance_spot", "BTC-USDT"): spot_book,
            ("okx_swap", "BTC-USDT-261225"): deriv_book,
        }

        scanner = BasisArbitrageScanner(
            registry=reg,
            health_gate=MarketHealthGate(self.policy),
            cost_engine=CostEngine(
                fee_schedules={
                    "binance_spot": FeeSchedule(venue_id="binance_spot", taker_fee_rate=Decimal("0.0010")),
                    "okx_swap": FeeSchedule(venue_id="okx_swap", taker_fee_rate=Decimal("0.0005")),
                }
            ),
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=self.policy,
            min_annualized_basis_bps=Decimal("500.0"),
        )

        # Target 0.0145 BTC -> aligned to OKX Swap contract multiplier (0.01 BTC/contract) -> 0.01 BTC spot & 1 contract deriv (`S2-002`, `S2-003`)
        spec = BasisPairSpec(
            pair_id="BTC_CASH_AND_CARRY",
            spot_venue_id="binance_spot",
            spot_instrument_id="BTC-USDT",
            deriv_venue_id="okx_swap",
            deriv_instrument_id="BTC-USDT-261225",
            target_base_qty=Decimal("0.0145"),
        )
        rep = scanner.evaluate_basis(spec, books, mark_price_reference=Decimal("70000"), now_utc=now)
        self.assertEqual(rep.opportunity.status, OpportunityStatus.PAPER)
        self.assertEqual(rep.spot_base_qty, Decimal("0.01"))
        self.assertEqual(rep.deriv_contract_qty, Decimal("1"))
        self.assertEqual(rep.net_delta_base_mismatch, Decimal("0"))
        # Verify S2-001: executable prices are 68000 (Spot Ask) and 68680 (Deriv Bid), NOT 70000 mark_price!
        self.assertEqual(rep.spot_executable_ask, Decimal("68000"))
        self.assertEqual(rep.deriv_executable_bid, Decimal("68680"))
        self.assertGreater(rep.annualized_net_basis_bps, Decimal("500"))

    def test_deriv_003_and_004_funding_rate_carry_margin_monitor_and_paper_closed_loop(self) -> None:
        now = datetime(2026, 10, 26, 5, 20, 0, tzinfo=timezone.utc)
        ledger = LedgerStore(":memory:")
        cfg_hash = ledger.record_config_version(self.settings)
        gateway = RiskGateway(self.settings, ledger)
        reg = InstrumentRegistry()

        reg.register(
            Instrument(
                venue_id="binance_spot",
                instrument_id="ETH-USDT",
                venue_symbol="ETHUSDT",
                instrument_type=InstrumentType.SPOT,
                base_asset="ETH",
                quote_asset="USDT",
                settlement_asset="USDT",
                contract_multiplier=Decimal("1"),
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_notional=Decimal("10"),
                min_qty=Decimal("0.001"),
                event_time_utc=now,
                receive_time_utc=now,
            )
        )
        reg.register(
            Instrument(
                venue_id="binance_usdm",
                instrument_id="ETH-USDT-PERP",
                venue_symbol="ETHUSDT",
                instrument_type=InstrumentType.PERPETUAL,
                base_asset="ETH",
                quote_asset="USDT",
                settlement_asset="USDT",
                contract_multiplier=Decimal("1"),
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_notional=Decimal("10"),
                min_qty=Decimal("0.001"),
                funding_interval_hours=8,
                event_time_utc=now,
                receive_time_utc=now,
            )
        )

        spot_book = OrderBookBuilder("binance_spot", "ETH-USDT")
        spot_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("2499.50"), quantity=Decimal("10"))],
            asks=[PriceLevel(price=Decimal("2500.00"), quantity=Decimal("10"))],
            last_update_id=10,
            event_time_utc=now,
            receive_time_utc=now,
        )
        perp_book = OrderBookBuilder("binance_usdm", "ETH-USDT-PERP")
        perp_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("2500.50"), quantity=Decimal("10"))],
            asks=[PriceLevel(price=Decimal("2501.00"), quantity=Decimal("10"))],
            last_update_id=20,
            event_time_utc=now,
            receive_time_utc=now,
        )
        books = {
            ("binance_spot", "ETH-USDT"): spot_book,
            ("binance_usdm", "ETH-USDT-PERP"): perp_book,
        }

        cost_engine = CostEngine(
            fee_schedules={
                "binance_spot": FeeSchedule(venue_id="binance_spot", taker_fee_rate=Decimal("0.0005")),
                "binance_usdm": FeeSchedule(venue_id="binance_usdm", taker_fee_rate=Decimal("0.0004")),
            }
        )
        funding_scanner = FundingRateArbitrageScanner(
            registry=reg,
            health_gate=MarketHealthGate(self.policy),
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(ledger),
            risk_policy=self.policy,
        )

        # 1. High positive funding rate (+15 bps per 8h, 4 settlements = +60 bps gross carry vs ~36 bps round-trip fees) -> APPROVED for PAPER (`S2-006`, `S2-008`)
        high_funding = DerivativeFundingSnapshot(
            venue_id="binance_usdm",
            instrument_id="ETH-USDT-PERP",
            mark_price=Decimal("2500.50"),
            index_price=Decimal("2500.00"),
            funding_rate=Decimal("0.0015"),  # 15 bps per 8h
            next_funding_time_utc=now + timedelta(hours=4),
            funding_interval_hours=8,
            event_time_utc=now,
            receive_time_utc=now,
        )
        f_spec = FundingCarryPairSpec(
            pair_id="ETH_SPOT_PERP_CARRY",
            long_venue_id="binance_spot",
            long_instrument_id="ETH-USDT",
            short_deriv_venue_id="binance_usdm",
            short_deriv_instrument_id="ETH-USDT-PERP",
            target_base_qty=Decimal("0.200"),
            expected_holding_settlements=4,
        )
        f_rep = funding_scanner.evaluate_funding_carry(f_spec, books, short_funding=high_funding, now_utc=now)
        self.assertEqual(f_rep.opportunity.status, OpportunityStatus.PAPER)
        self.assertEqual(f_rep.standardized_8h_net_funding_bps, Decimal("15.0000"))
        self.assertGreater(f_rep.expected_net_carry_usdt, Decimal("0"))

        # 2. Cross-venue funding settlement clock mismatch (`S2-007`) -> rejected with FUNDING_SETTLEMENT_TIME_MISMATCH
        mismatched_long_funding = DerivativeFundingSnapshot(
            venue_id="okx_swap",
            instrument_id="ETH-USDT-PERP",
            mark_price=Decimal("2500.00"),
            index_price=Decimal("2500.00"),
            funding_rate=Decimal("0.0001"),
            next_funding_time_utc=now + timedelta(hours=2),  # 2h skew from short_funding!
            funding_interval_hours=4,
            event_time_utc=now,
            receive_time_utc=now,
        )
        f_rep_skew = funding_scanner.evaluate_funding_carry(
            f_spec, books, short_funding=high_funding, long_funding=mismatched_long_funding, now_utc=now
        )
        self.assertEqual(f_rep_skew.opportunity.status, OpportunityStatus.REJECTED)
        self.assertIn("FUNDING_SETTLEMENT_TIME_MISMATCH", f_rep_skew.opportunity.reject_reason_codes)

        # 3. Execute the approved S2-B opportunity end-to-end in PaperExecutionSimulator (`DERIV-004`)
        paper_spot = PaperVenueAdapter(
            "binance_spot", reg, cost_engine, {"ETH-USDT": spot_book}, {"USDT": Decimal("5000"), "ETH": Decimal("1.0")}
        )
        paper_usdm = PaperVenueAdapter(
            "binance_usdm", reg, cost_engine, {"ETH-USDT-PERP": perp_book}, {"USDT": Decimal("5000"), "ETH": Decimal("1.0")}
        )
        venues = {"binance_spot": paper_spot, "binance_usdm": paper_usdm}
        orch = ExecutionOrchestrator(ledger, gateway, venues)
        sim = PaperExecutionSimulator(orch, ledger, venues)

        open_plan = ExecutionPlan(
            cycle_id="cyc_s2b_open_1",
            opportunity_id=f_rep.opportunity.opportunity_id,
            strategy_id=f_rep.opportunity.strategy_id,
            strategy_version=f_rep.opportunity.strategy_version,
            execution_mode="paper",
            state=CycleState.RESERVED,
            legs=[
                ExecutionPlanLeg(
                    client_order_id="cid_s2b_spot_buy",
                    venue_id="binance_spot",
                    instrument_id="ETH-USDT",
                    side=OrderSide.BUY,
                    order_type=OrderType.IOC,
                    quantity=Decimal("0.200"),
                    limit_price=Decimal("2505.00"),
                    max_slippage_bps=Decimal("10"),
                    rules_version="v1",
                ),
                ExecutionPlanLeg(
                    client_order_id="cid_s2b_perp_sell",
                    venue_id="binance_usdm",
                    instrument_id="ETH-USDT-PERP",
                    side=OrderSide.SELL,
                    order_type=OrderType.IOC,
                    quantity=Decimal("0.200"),
                    limit_price=Decimal("2495.00"),
                    max_slippage_bps=Decimal("10"),
                    rules_version="v1",
                ),
            ],
            reserved_asset="USDT",
            reserved_amount=Decimal("500.00"),
            max_unhedged_notional=Decimal("500.00"),
            config_version_hash=cfg_hash,
        )
        rd = RiskDecisionRecord(
            cycle_id=open_plan.cycle_id,
            opportunity_id=open_plan.opportunity_id,
            strategy_id=open_plan.strategy_id,
            policy_version=self.policy.policy_version,
            approved=True,
            reason_codes=["PASS"],
        )
        orch.prepare_cycle_outbox(open_plan, rd)
        open_res = asyncio.run(sim.execute_plan(open_plan))
        self.assertEqual(open_res.cycle_state, CycleState.SETTLED)
        self.assertEqual(open_res.unhedged_base_exposure, Decimal("0"))

        # 4. Verify MarginAndLiquidationMonitor triggers CRITICAL_AUTO_DELEVERAGE and generates paired 2-leg unwind plan (`S2-009`, `S2-010`)
        monitor = MarginAndLiquidationMonitor()
        danger_pos = PositionSnapshot(
            venue_id="binance_usdm",
            account_id="usdm_main",
            instrument_id="ETH-USDT-PERP",
            position_side="SHORT",
            quantity=Decimal("-2.0"),
            contract_multiplier=Decimal("1"),
            entry_price=Decimal("2500.00"),
            mark_price=Decimal("2650.00"),
            unrealized_pnl=Decimal("-300.00"),
            margin_used=Decimal("850.00"),
            liquidation_price=Decimal("2780.00"),  # Only ~490 bps away from mark_price (< 800 bps critical threshold!)
            event_time_utc=now,
            receive_time_utc=now,
        )
        m_rep = monitor.evaluate_position_health(
            deriv_position=danger_pos,
            account_equity_usdt=Decimal("1000.00"),
            paired_spot_venue_id="binance_spot",
            paired_spot_instrument_id="ETH-USDT",
            spot_executable_bid=Decimal("2499.50"),
            deriv_executable_ask=Decimal("2501.00"),
            config_version_hash=cfg_hash,
        )
        self.assertEqual(m_rep.action, MarginSafetyAction.CRITICAL_AUTO_DELEVERAGE)
        self.assertFalse(m_rep.allow_new_open)
        self.assertTrue(m_rep.requires_emergency_unwind)
        self.assertIsNotNone(m_rep.emergency_unwind_plan)
        # Paired unwind plan MUST have 2 legs (close short perp + sell hedged spot) with equal base delta!
        self.assertEqual(len(m_rep.emergency_unwind_plan.legs), 2)
        self.assertEqual(m_rep.emergency_unwind_plan.legs[0].side, OrderSide.BUY)
        self.assertEqual(m_rep.emergency_unwind_plan.legs[1].side, OrderSide.SELL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
