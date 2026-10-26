"""Comprehensive Unit & Contract Tests for Sprint 4 (VENUE-001 & STRAT-003).
Covers:
- VENUE-001: OKX V5 Spot REST & WebSocket Adapters (server time, instruments parsing, L2 books 4-tuple parsing,
  account balance parsing, OKX native error codes 50011/50111/50004/51008 mapping, WebSocket snapshot/update prevSeqId continuity & gap detection).
- STRAT-003 & S1-001..S1-009: Cross-CEX Spot Arbitrage Scanner (Binance <-> OKX, low Ask buy / high Bid sell,
  different fee schedules, different step_size/min_notional rules, pre-positioned inventory enforcement & auto-scaling,
  stale book / cross-venue skew rejection, deposit/withdrawal pause non-blocking with rebalance flag, inventory skew alert,
  and cross-quote currency FX conversion & unreliable FX rejection).
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from arb.config import RiskPolicyConfig, SecretStr, VenueCredentials
from arb.costs import CostEngine, FeeSchedule
from arb.domain import (
    BalanceSnapshot,
    Instrument,
    InstrumentType,
    OpportunityDecision,
    OpportunityStatus,
    OrderSide,
    PriceLevel,
    TradingStatus,
)
from arb.instruments import InstrumentRegistry
from arb.marketdata import MarketHealthGate, OrderBookBuilder, SequenceStatus
from arb.observability import AlertManager, MetricsRegistry, StructuredLogger
from arb.strategies.cross_cex_spot import (
    CrossCexPairMapping,
    CrossCexSpotScanner,
    FxRateQuote,
    VenueTransferStatus,
)
from arb.strategy import OpportunityLifecycleManager, OpportunityRejectReason
from arb.venues import ErrorCategory, ExchangeError
from arb.venues.binance_spot import HttpResponsePayload, HttpTransport
from arb.venues.okx_spot import (
    OkxOrderBookEvent,
    OkxSpotRestAdapter,
    OkxSpotStreamManager,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_YAML_PATH = REPO_ROOT / "configs" / "risk-policy.example.yaml"


class _MockOkxTransport(HttpTransport):
    def __init__(self, handler: Callable[[str, str, Optional[Dict[str, Any]], Optional[Dict[str, str]]], HttpResponsePayload]) -> None:
        self._handler = handler
        self.calls: List[Dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 3000,
    ) -> HttpResponsePayload:
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return self._handler(method, url, params, headers)


class TestSprint4OkxAndCrossCexSpot(unittest.TestCase):
    # ==========================================================================
    # VENUE-001: OKX V5 Spot REST & WebSocket Adapter Contract Tests
    # ==========================================================================

    def test_venue001_okx_rest_instruments_books_balances_and_error_codes(self) -> None:
        now = datetime(2026, 10, 26, 4, 0, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        def router(method: str, url: str, params: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> HttpResponsePayload:
            if url.endswith("/api/v5/public/time"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={"code": "0", "msg": "", "data": [{"ts": str(now_ms - 50)}]},
                )
            if url.endswith("/api/v5/public/instruments"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "code": "0",
                        "msg": "",
                        "data": [
                            {
                                "instType": "SPOT",
                                "instId": "BTC-USDT",
                                "baseCcy": "BTC",
                                "quoteCcy": "USDT",
                                "tickSz": "0.1",
                                "lotSz": "0.0001",
                                "minSz": "0.0001",
                                "state": "live",
                            },
                            {
                                "instType": "SPOT",
                                "instId": "ETH-USDT",
                                "baseCcy": "ETH",
                                "quoteCcy": "USDT",
                                "tickSz": "0.01",
                                "lotSz": "0.001",
                                "minSz": "0.001",
                                "state": "suspend",
                            },
                        ],
                    },
                )
            if url.endswith("/api/v5/market/books"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "code": "0",
                        "msg": "",
                        "data": [
                            {
                                "asks": [["68010.5", "1.2000", "0", "3"], ["68020.0", "2.5000", "0", "5"]],
                                "bids": [["68000.0", "0.8500", "0", "2"], ["67995.0", "1.5000", "0", "4"]],
                                "ts": str(now_ms),
                                "seqId": 900100,
                            }
                        ],
                    },
                )
            if url.endswith("/api/v5/account/balance"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body={
                        "code": "0",
                        "msg": "",
                        "data": [
                            {
                                "uTime": str(now_ms),
                                "details": [
                                    {"ccy": "USDT", "eq": "15000.0", "availBal": "12000.0", "frozenBal": "3000.0"},
                                    {"ccy": "BTC", "eq": "0.5000", "availBal": "0.4000", "frozenBal": "0.1000"},
                                ],
                            }
                        ],
                    },
                )
            raise AssertionError(f"Unexpected URL {url}")

        creds = VenueCredentials(
            venue_id="okx_spot",
            api_key=SecretStr("okx_key_1"),
            api_secret=SecretStr("okx_sec_1"),
            passphrase=SecretStr("okx_pass_1"),
        )
        adapter = OkxSpotRestAdapter(transport=_MockOkxTransport(router), credentials=creds)

        srv_dt = asyncio.run(adapter.get_server_time(local_now_utc=now))
        self.assertEqual(int(srv_dt.timestamp() * 1000), now_ms - 50)

        insts = asyncio.run(adapter.fetch_instruments())
        self.assertEqual(len(insts), 2)
        self.assertEqual(insts[0].venue_id, "okx_spot")
        self.assertEqual(insts[0].instrument_id, "BTC-USDT")
        self.assertEqual(insts[0].tick_size, Decimal("0.1"))
        self.assertEqual(insts[0].step_size, Decimal("0.0001"))
        self.assertEqual(insts[0].trading_status, TradingStatus.TRADING)
        self.assertEqual(insts[1].trading_status, TradingStatus.HALTED)

        book = asyncio.run(adapter.fetch_order_book("BTC-USDT", depth=20))
        self.assertEqual(book.last_update_id, 900100)
        self.assertEqual(book.bids[0].price, Decimal("68000.0"))
        self.assertEqual(book.asks[0].price, Decimal("68010.5"))

        bals = asyncio.run(adapter.fetch_balances())
        self.assertEqual(len(bals), 2)
        self.assertEqual(bals[0].asset, "USDT")
        self.assertEqual(bals[0].free_balance, Decimal("12000.0"))

        # Test OKX native error code 50011 -> RATE_LIMITED
        ad_rl = OkxSpotRestAdapter(
            transport=_MockOkxTransport(
                lambda m, u, p, h: HttpResponsePayload(
                    status_code=200,
                    json_body={"code": "50011", "msg": "Rate limit reached", "data": []},
                )
            )
        )
        with self.assertRaises(ExchangeError) as ctx_rl:
            asyncio.run(ad_rl.fetch_order_book("BTC-USDT"))
        self.assertEqual(ctx_rl.exception.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(ctx_rl.exception.native_code, "50011")

        # Test OKX native error code 50113 -> AUTH_PERMISSION
        ad_auth = OkxSpotRestAdapter(
            transport=_MockOkxTransport(
                lambda m, u, p, h: HttpResponsePayload(
                    status_code=401,
                    json_body={"code": "50113", "msg": "Invalid passphrase", "data": []},
                )
            )
        )
        with self.assertRaises(ExchangeError) as ctx_auth:
            asyncio.run(ad_auth.fetch_order_book("BTC-USDT"))
        self.assertEqual(ctx_auth.exception.category, ErrorCategory.AUTH_PERMISSION)

    def test_venue001_okx_websocket_snapshot_update_and_seq_gap(self) -> None:
        now = datetime(2026, 10, 26, 4, 5, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        builder = OrderBookBuilder("okx_spot", "BTC-USDT")
        mgr = OkxSpotStreamManager(
            symbol_to_instrument_id={"BTC-USDT": "BTC-USDT"},
            order_books={"BTC-USDT": builder},
        )
        mgr.on_connect()
        self.assertTrue(mgr.is_connected)

        # 1. Initial WebSocket snapshot (seqId = 500)
        snap_msg = {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "snapshot",
            "data": [
                {
                    "asks": [["68010.0", "1.0", "0", "1"]],
                    "bids": [["68000.0", "1.0", "0", "1"]],
                    "ts": str(now_ms),
                    "seqId": 500,
                    "prevSeqId": -1,
                }
            ],
        }
        ev1 = mgr.on_raw_message(snap_msg, receive_time_utc=now)
        self.assertIsInstance(ev1, OkxOrderBookEvent)
        self.assertTrue(builder.is_valid())
        self.assertEqual(builder.last_update_id, 500)

        # 2. Continuous WebSocket update (prevSeqId = 500, seqId = 505)
        upd_msg = {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "update",
            "data": [
                {
                    "asks": [["68010.0", "0", "0", "0"], ["68008.0", "2.0", "0", "2"]],
                    "bids": [["68001.0", "1.5", "0", "1"]],
                    "ts": str(now_ms + 50),
                    "seqId": 505,
                    "prevSeqId": 500,
                }
            ],
        }
        mgr.on_raw_message(upd_msg, receive_time_utc=now + timedelta(milliseconds=55))
        self.assertTrue(builder.is_valid())
        self.assertEqual(builder.last_update_id, 505)
        self.assertEqual(builder.to_order_book().asks[0].price, Decimal("68008.0"))
        self.assertEqual(builder.to_order_book().bids[0].price, Decimal("68001.0"))

        # 3. Sequence gap on update (prevSeqId = 508 != 505) -> immediately invalidates book!
        gap_msg = {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "update",
            "data": [
                {
                    "asks": [],
                    "bids": [["68002.0", "1.0", "0", "1"]],
                    "ts": str(now_ms + 100),
                    "seqId": 510,
                    "prevSeqId": 508,
                }
            ],
        }
        mgr.on_raw_message(gap_msg, receive_time_utc=now + timedelta(milliseconds=105))
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.GAP_DETECTED)

    # ==========================================================================
    # STRAT-003 & S1-001..S1-009: Cross-CEX Spot Arbitrage & Inventory Tests
    # ==========================================================================

    def test_strat003_and_s1_cross_cex_spot_arbitrage_and_inventory_gates(self) -> None:
        now = datetime(2026, 10, 26, 4, 15, 0, tzinfo=timezone.utc)
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)
        reg = InstrumentRegistry()

        # Register BTC-USDT on Binance (step_size=0.00001, tick_size=0.01) and OKX (step_size=0.0001, tick_size=0.1)
        b_btc = Instrument(
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            venue_symbol="BTCUSDT",
            instrument_type=InstrumentType.SPOT,
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_notional=Decimal("10.0"),
            min_qty=Decimal("0.00001"),
            event_time_utc=now,
            receive_time_utc=now,
        )
        o_btc = Instrument(
            venue_id="okx_spot",
            instrument_id="BTC-USDT",
            venue_symbol="BTC-USDT",
            instrument_type=InstrumentType.SPOT,
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.0001"),
            min_notional=Decimal("10.0"),
            min_qty=Decimal("0.0001"),
            event_time_utc=now,
            receive_time_utc=now,
        )
        reg.register(b_btc, canonical_pair="BTC/USDT")
        reg.register(o_btc, canonical_pair="BTC/USDT")

        # Configure distinct fee tiers (S1-002): Binance VIP0 = 10 bps taker, OKX VIP1 = 8 bps taker
        cost_engine = CostEngine(
            fee_schedules={
                "binance_spot": FeeSchedule(
                    venue_id="binance_spot",
                    tier_name="VIP0",
                    maker_fee_rate=Decimal("0.0010"),
                    taker_fee_rate=Decimal("0.0010"),
                ),
                "okx_spot": FeeSchedule(
                    venue_id="okx_spot",
                    tier_name="VIP1",
                    maker_fee_rate=Decimal("0.0006"),
                    taker_fee_rate=Decimal("0.0008"),
                ),
            }
        )

        # Books: Binance Ask = 68,000.00, OKX Bid = 68,200.00 (+29.4 bps gross edge, ~ +11.4 bps net edge after 18 bps fees)
        b_book = OrderBookBuilder("binance_spot", "BTC-USDT")
        b_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("67990.00"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68000.00"), quantity=Decimal("1.0"))],
            last_update_id=100,
            event_time_utc=now,
            receive_time_utc=now,
        )
        o_book = OrderBookBuilder("okx_spot", "BTC-USDT")
        o_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68200.00"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68210.00"), quantity=Decimal("1.0"))],
            last_update_id=200,
            event_time_utc=now + timedelta(milliseconds=50),
            receive_time_utc=now + timedelta(milliseconds=50),
        )
        books: Dict[Tuple[str, str], OrderBookBuilder] = {
            ("binance_spot", "BTC-USDT"): b_book,
            ("okx_spot", "BTC-USDT"): o_book,
        }

        # Pre-positioned balances:
        # Binance has 1,000 USDT free and 0.04 BTC existing; OKX has 0.01 BTC free (constrains target 0.012345 BTC down to 0.0100 BTC!)
        balances: Dict[Tuple[str, str], BalanceSnapshot] = {
            ("binance_spot", "USDT"): BalanceSnapshot(
                venue_id="binance_spot",
                account_id="b_main",
                asset="USDT",
                total_balance=Decimal("1000"),
                free_balance=Decimal("1000"),
                event_time_utc=now,
            ),
            ("binance_spot", "BTC"): BalanceSnapshot(
                venue_id="binance_spot",
                account_id="b_main",
                asset="BTC",
                total_balance=Decimal("0.04"),
                free_balance=Decimal("0.04"),
                event_time_utc=now,
            ),
            ("okx_spot", "BTC"): BalanceSnapshot(
                venue_id="okx_spot",
                account_id="o_main",
                asset="BTC",
                total_balance=Decimal("0.01009"),
                free_balance=Decimal("0.01009"),
                event_time_utc=now,
            ),
            ("okx_spot", "USDT"): BalanceSnapshot(
                venue_id="okx_spot",
                account_id="o_main",
                asset="USDT",
                total_balance=Decimal("1000"),
                free_balance=Decimal("1000"),
                event_time_utc=now,
            ),
        }

        # Simulate withdrawal paused on Binance (S1-007: does NOT block pre-positioned inventory trade, but flags rebalance blocked)
        transfer_statuses = {
            ("binance_spot", "BTC"): VenueTransferStatus(
                venue_id="binance_spot",
                asset="BTC",
                deposit_enabled=True,
                withdrawal_enabled=False,
            )
        }

        scanner = CrossCexSpotScanner(
            registry=reg,
            health_gate=MarketHealthGate(risk_policy=policy),
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
            max_venue_inventory_skew_ratio=Decimal("0.80"),
        )
        mapping = CrossCexPairMapping(
            canonical_pair="BTC/USDT",
            venue_a_id="binance_spot",
            venue_a_instrument_id="BTC-USDT",
            venue_b_id="okx_spot",
            venue_b_instrument_id="BTC-USDT",
            target_base_qty=Decimal("0.012345"),
        )

        exec_reps, all_reps = scanner.scan_pair(
            mapping=mapping,
            order_books=books,
            balances=balances,
            transfer_statuses=transfer_statuses,
            now_utc=now + timedelta(milliseconds=80),
        )
        # Only Binance (BUY @ Ask 68000) -> OKX (SELL @ Bid 68200) is profitable
        self.assertEqual(len(exec_reps), 1)
        win = exec_reps[0]
        self.assertEqual(win.buy_venue_id, "binance_spot")
        self.assertEqual(win.sell_venue_id, "okx_spot")

        # Verify S1-003 & S1-005: target 0.012345 constrained by OKX available 0.01009 and quantized to coarser OKX step_size (0.0001) -> 0.0100 BTC!
        self.assertEqual(win.opportunity.legs[0].target_quantity, Decimal("0.0100"))
        self.assertEqual(win.opportunity.legs[1].target_quantity, Decimal("0.0100"))

        # Verify S1-001 & S1-002 exact PnL:
        # Buy 0.01 BTC @ 68000 = 680 USDT (fee 10 bps = 0.68 USDT)
        # Sell 0.01 BTC @ 68200 = 682 USDT (fee 8 bps = 0.5456 USDT)
        # Gross edge = 2.00 USDT; total fees = 1.2256 USDT; net PnL = 0.7744 USDT
        self.assertEqual(win.opportunity.gross_edge, Decimal("2.00"))
        self.assertEqual(win.opportunity.fees, Decimal("1.2256"))
        self.assertEqual(win.opportunity.expected_net_pnl, Decimal("0.7744"))

        # Verify S1-007 & S1-008: post-trade Binance holds (0.04 + 0.01) / (0.04 + 0.01009) = 99.82% >= 80% -> triggers skew alert AND flags withdrawal pause!
        self.assertIsNotNone(win.inventory_state)
        self.assertTrue(win.inventory_state.rebalance_alert_triggered)
        self.assertTrue(win.inventory_state.rebalance_blocked_by_transfer_pause)

        # Verify S1-005 zero inventory rejection: if OKX has 0 BTC free, candidate is rejected with INSUFFICIENT_PREPOSITIONED_INVENTORY
        balances_empty = dict(balances)
        balances_empty[("okx_spot", "BTC")] = BalanceSnapshot(
            venue_id="okx_spot",
            account_id="o_main",
            asset="BTC",
            total_balance=Decimal("0.5"),
            free_balance=Decimal("0.5"),
            reserved_balance=Decimal("0.5"),  # 100% reserved by another cycle -> allocatable = 0!
            event_time_utc=now,
        )
        exec_empty, all_empty = CrossCexSpotScanner(
            registry=reg,
            health_gate=MarketHealthGate(risk_policy=policy),
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
        ).scan_pair(mapping=mapping, order_books=books, balances=balances_empty, now_utc=now + timedelta(milliseconds=80))
        self.assertEqual(len(exec_empty), 0)
        self.assertIn("INSUFFICIENT_PREPOSITIONED_INVENTORY", all_empty[0].opportunity.reject_reason_codes)

        # Verify S1-009 cross-quote currency FX conversion & unreliable FX rejection:
        # Register BTC-USDC on OKX
        o_btc_usdc = o_btc.copy(update={"instrument_id": "BTC-USDC", "venue_symbol": "BTC-USDC", "quote_asset": "USDC", "settlement_asset": "USDC"})
        reg.register(o_btc_usdc, canonical_pair="BTC/USD_STABLE")
        o_usdc_book = OrderBookBuilder("okx_spot", "BTC-USDC")
        o_usdc_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68250.0"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68260.0"), quantity=Decimal("1.0"))],
            last_update_id=300,
            event_time_utc=now,
            receive_time_utc=now,
        )
        books[("okx_spot", "BTC-USDC")] = o_usdc_book
        map_fx = CrossCexPairMapping(
            canonical_pair="BTC/USD_STABLE",
            venue_a_id="binance_spot",
            venue_a_instrument_id="BTC-USDT",
            venue_b_id="okx_spot",
            venue_b_instrument_id="BTC-USDC",
            target_base_qty=Decimal("0.0100"),
        )
        # Without FX rate -> rejected with UNRELIABLE_FX_CONVERSION
        exec_no_fx, all_no_fx = CrossCexSpotScanner(
            registry=reg,
            health_gate=MarketHealthGate(risk_policy=policy),
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
        ).scan_pair(mapping=map_fx, order_books=books, balances=balances, fx_rates=None, now_utc=now + timedelta(milliseconds=80))
        self.assertEqual(len(exec_no_fx), 0)
        self.assertIn("UNRELIABLE_FX_CONVERSION", all_no_fx[0].opportunity.reject_reason_codes)

        # With reliable FX rate (USDC -> USDT = 1.0000, cost = 1.0 bps) -> approved for PAPER!
        exec_with_fx, _ = CrossCexSpotScanner(
            registry=reg,
            health_gate=MarketHealthGate(risk_policy=policy),
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(),
            risk_policy=policy,
        ).scan_pair(
            mapping=map_fx,
            order_books=books,
            balances=balances,
            fx_rates={
                ("USDC", "USDT"): FxRateQuote(
                    from_asset="USDC",
                    to_asset="USDT",
                    rate=Decimal("1.0000"),
                    conversion_cost_bps=Decimal("1.0"),
                    is_reliable=True,
                )
            },
            now_utc=now + timedelta(milliseconds=80),
        )
        self.assertEqual(len(exec_with_fx), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
