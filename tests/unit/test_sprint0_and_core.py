"""Comprehensive Unit & Contract Tests for Sprint 0 (META-001~002) and Sprint 1 (CORE-001~007).
Compatible with both `python3 -m unittest` and `pytest`.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from arb.config import (
    AppEnvironment,
    ConfigurationError,
    ExecutionMode,
    FeatureFlags,
    RiskPolicyConfig,
    SecretStr,
    Settings,
    VenueCredentials,
    mask_sensitive_text,
)
from arb.domain import (
    BalanceSnapshot,
    ChainTransaction,
    ChainTxStatus,
    CycleState,
    DataHealthStatus,
    ExecutionPlan,
    ExecutionPlanLeg,
    Fill,
    Instrument,
    InstrumentType,
    MarketSnapshot,
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OptionType,
    OrderBook,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    PriceLevel,
    PriceType,
    TradingStatus,
)
from arb.instruments import (
    InstrumentNotTradableError,
    InstrumentRegistry,
    RulesExpiredError,
    TradingRules,
)
from arb.ledger import (
    LedgerIntegrityError,
    LedgerStore,
    RiskDecisionRecord,
)
from arb.observability import (
    AlertManager,
    AlertSeverity,
    MetricsRegistry,
    StructuredLogger,
    TraceContext,
)
from arb.venues import (
    AccountAdapter,
    ErrorCategory,
    ExchangeError,
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    TradingAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_YAML_PATH = REPO_ROOT / "configs" / "risk-policy.example.yaml"


class DummyVenueAdapter(
    InstrumentMetadataAdapter, MarketDataAdapter, AccountAdapter, TradingAdapter
):
    @property
    def venue_id(self) -> str:
        return "okx_spot"

    async def get_server_time(self) -> datetime:
        return datetime.now(timezone.utc)

    async def fetch_instruments(self) -> List[Instrument]:
        return []

    async def fetch_order_book(self, instrument_id: str, depth: int = 20) -> OrderBook:
        return OrderBook(
            venue_id=self.venue_id,
            instrument_id=instrument_id,
            bids=[PriceLevel(price=Decimal("67990"), quantity=Decimal("1.5"))],
            asks=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.2"))],
            last_update_id=555,
        )

    async def fetch_market_snapshot(self, instrument_id: str) -> MarketSnapshot:
        return MarketSnapshot(
            venue_id=self.venue_id,
            instrument_id=instrument_id,
            quote_asset="USDT",
            bid_levels=[PriceLevel(price=Decimal("67990"), quantity=Decimal("1.5"))],
            ask_levels=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.2"))],
            sequence_id=555,
        )

    async def fetch_balances(self) -> List[BalanceSnapshot]:
        return []

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
        return OrderEvent(
            cycle_id=cycle_id,
            strategy_id=strategy_id,
            venue_id=self.venue_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            side=side,
            order_type=order_type,
            status=OrderStatus.UNKNOWN,
            native_venue_status="REQUEST_TIMEOUT_50000",
            order_quantity=quantity,
            limit_price=limit_price,
        )

    async def query_order(
        self, instrument_id: str, client_order_id: str, venue_order_id: Optional[str] = None
    ) -> OrderEvent:
        return OrderEvent(
            cycle_id="cyc_1",
            strategy_id="strat_s1",
            venue_id=self.venue_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id="okx_ord_999",
            side=OrderSide.BUY,
            order_type=OrderType.IOC,
            status=OrderStatus.FILLED,
            native_venue_status="filled",
            order_quantity=Decimal("0.1"),
            cumulative_filled_qty=Decimal("0.1"),
            average_fill_price=Decimal("68000"),
        )

    async def cancel_order(
        self, instrument_id: str, client_order_id: str, venue_order_id: Optional[str] = None
    ) -> OrderEvent:
        raise NotImplementedError

    async def fetch_fills(
        self, instrument_id: str, client_order_id: Optional[str] = None
    ) -> List[Fill]:
        return []


class TestSprint0AndSprint1Core(unittest.TestCase):
    def test_meta001_and_meta002_risk_policy_and_venue_capabilities(self) -> None:
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)
        self.assertEqual(policy.policy_version, "1.0.0")
        self.assertEqual(policy.max_single_order_notional, Decimal("1000.00"))
        self.assertEqual(policy.max_cycle_notional, Decimal("2000.00"))
        self.assertEqual(policy.max_book_age_ms, 500)
        self.assertEqual(policy.min_net_edge_bps, Decimal("5.0"))

        settings = Settings.from_env(RISK_YAML_PATH)
        self.assertFalse(settings.feature_flags.live_trading_enabled)
        self.assertEqual(settings.feature_flags.execution_mode, ExecutionMode.PAPER)

        # Approved CEX venues for paper trading: Binance + OKX
        self.assertTrue(settings.is_venue_executable("binance_spot", ExecutionMode.PAPER))
        self.assertTrue(settings.is_venue_executable("okx_spot", ExecutionMode.PAPER))
        self.assertTrue(settings.is_venue_executable("binance_usdm", ExecutionMode.PAPER))
        self.assertTrue(settings.is_venue_executable("okx_swap", ExecutionMode.PAPER))

        # Live mode must be blocked for all venues by default
        self.assertFalse(settings.is_venue_executable("binance_spot", ExecutionMode.LIVE))
        self.assertFalse(settings.is_venue_executable("okx_spot", ExecutionMode.LIVE))

        # S3 & S4 expansion modules must be strictly locked to research_only
        for research_mod in ("equity_broker", "evm_dex", "crypto_options", "cross_chain_bridge"):
            self.assertFalse(settings.is_venue_executable(research_mod, ExecutionMode.PAPER))
            self.assertFalse(settings.is_venue_executable(research_mod, ExecutionMode.LIVE))

    def test_core002_live_trading_cannot_be_accidentally_enabled_in_paper_env(self) -> None:
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)
        with self.assertRaisesRegex(ConfigurationError, "Cannot enable LIVE trading in 'paper'"):
            Settings(
                app_env=AppEnvironment.PAPER,
                risk_policy=policy,
                feature_flags=FeatureFlags(
                    live_trading_enabled=True,
                    execution_mode=ExecutionMode.LIVE,
                    live_approval_ticket_id="TICKET-999999",
                ),
            )

        with self.assertRaisesRegex(ConfigurationError, "live_approval_ticket_id"):
            Settings(
                app_env=AppEnvironment.PRODUCTION,
                risk_policy=policy,
                feature_flags=FeatureFlags(
                    live_trading_enabled=True,
                    execution_mode=ExecutionMode.LIVE,
                    live_approval_ticket_id=None,
                ),
            )

    def test_core002_secret_redaction_never_leaks_credentials(self) -> None:
        raw_secret = "SUPER_SECRET_OKX_PRIVATE_KEY_987654321"
        raw_pass = "MY_OKX_PASSPHRASE_12345"
        settings = Settings.from_env(
            RISK_YAML_PATH,
            env_override={
                "OKX_API_KEY": "okx_key_abc",
                "OKX_API_SECRET": raw_secret,
                "OKX_PASSPHRASE": raw_pass,
            },
        )
        cred = settings.credentials["okx_spot"]
        self.assertNotIn(raw_secret, repr(cred))
        self.assertNotIn(raw_secret, str(cred.api_secret))
        self.assertEqual(cred.api_secret.get_secret_value(), raw_secret)

        safe_dump = json.dumps(settings.to_safe_dict())
        self.assertNotIn(raw_secret, safe_dump)
        self.assertNotIn(raw_pass, safe_dump)
        self.assertIn("***REDACTED***", safe_dump)

    def test_core003_utc_enforcement_and_price_type_separation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Naive datetime"):
            Instrument(
                venue_id="binance_spot",
                instrument_id="BTC-USDT",
                venue_symbol="BTCUSDT",
                instrument_type=InstrumentType.SPOT,
                base_asset="BTC",
                quote_asset="USDT",
                settlement_asset="USDT",
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.00001"),
                min_notional=Decimal("10"),
                event_time_utc=datetime(2026, 10, 26, 10, 0, 0),
            )

        with self.assertRaisesRegex(ValueError, "strictly forbidden for execution"):
            OpportunityLeg(
                venue_id="binance_usdm",
                instrument_id="BTC-USDT-PERP",
                side=OrderSide.BUY,
                target_quantity=Decimal("0.1"),
                executable_price=Decimal("68000.0"),
                executable_price_type=PriceType.MARK_PRICE,
                available_depth_qty=Decimal("2.5"),
                expected_fee=Decimal("2.72"),
                fee_asset="USDT",
                rules_version="v1",
            )

        with self.assertRaisesRegex(ValueError, "BUY leg cannot use BEST_BID"):
            OpportunityLeg(
                venue_id="binance_spot",
                instrument_id="BTC-USDT",
                side=OrderSide.BUY,
                target_quantity=Decimal("0.1"),
                executable_price=Decimal("68000.0"),
                executable_price_type=PriceType.BEST_BID,
                available_depth_qty=Decimal("1.0"),
                expected_fee=Decimal("0.68"),
                fee_asset="USDT",
                rules_version="v1",
            )

    def test_core003_orderbook_crossing_and_balance_allocatable(self) -> None:
        with self.assertRaisesRegex(ValueError, "Crossed order book"):
            OrderBook(
                venue_id="okx_spot",
                instrument_id="BTC-USDT",
                bids=[PriceLevel(price=Decimal("68010"), quantity=Decimal("1.0"))],
                asks=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0"))],
                last_update_id=101,
                is_valid=True,
            )

        bal = BalanceSnapshot(
            venue_id="okx_spot",
            account_id="acct_main",
            asset="USDT",
            total_balance=Decimal("10000"),
            free_balance=Decimal("6000"),
            locked_balance=Decimal("2000"),
            reserved_balance=Decimal("1500"),
            in_transit_balance=Decimal("2000"),
        )
        self.assertEqual(bal.allocatable_balance, Decimal("4500"))

    def test_core004_adapter_error_taxonomy_and_contract(self) -> None:
        timeout_err = ExchangeError(
            venue_id="binance_spot",
            category=ErrorCategory.UNKNOWN_ORDER_STATE,
            message="ReadTimeout waiting for POST /api/v3/order",
            http_status=504,
            native_code="-1007",
            is_write_operation=True,
        )
        self.assertFalse(timeout_err.is_definite_failure)
        self.assertTrue(timeout_err.requires_reconciliation)
        self.assertFalse(timeout_err.is_retryable_readonly)

        reject_err = ExchangeError(
            venue_id="binance_spot",
            category=ErrorCategory.DEFINITE_REJECT,
            message="Filter failure: MIN_NOTIONAL",
            http_status=400,
            native_code="-1013",
            is_write_operation=True,
        )
        self.assertTrue(reject_err.is_definite_failure)
        self.assertFalse(reject_err.requires_reconciliation)

        adapter = DummyVenueAdapter()
        ev = asyncio.run(
            adapter.submit_order(
                cycle_id="cyc_1",
                strategy_id="strat_s1",
                instrument_id="BTC-USDT",
                client_order_id="cid_001",
                side=OrderSide.BUY,
                order_type=OrderType.IOC,
                quantity=Decimal("0.1"),
                limit_price=Decimal("68000"),
            )
        )
        self.assertEqual(ev.status, OrderStatus.UNKNOWN)
        self.assertEqual(ev.native_venue_status, "REQUEST_TIMEOUT_50000")

    def test_core005_instrument_registry_and_multiplier_quantization(self) -> None:
        reg = InstrumentRegistry(max_rules_age_seconds=600)
        now = datetime.now(timezone.utc)

        b_inst = Instrument(
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            venue_symbol="BTCUSDT",
            instrument_type=InstrumentType.SPOT,
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_multiplier=Decimal("1"),
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_notional=Decimal("10.0"),
            min_qty=Decimal("0.00001"),
            receive_time_utc=now,
        )
        o_inst = Instrument(
            venue_id="okx_swap",
            instrument_id="BTC-USDT-SWAP",
            venue_symbol="BTC-USDT-SWAP",
            instrument_type=InstrumentType.PERPETUAL,
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_multiplier=Decimal("0.01"),
            tick_size=Decimal("0.1"),
            step_size=Decimal("1"),
            min_notional=Decimal("100.0"),
            min_qty=Decimal("1"),
            receive_time_utc=now,
        )

        b_rules = reg.register(b_inst, canonical_pair="BTC/USDT")
        o_rules = reg.register(o_inst, canonical_pair="BTC/USDT")
        self.assertNotEqual(b_rules.rules_version, o_rules.rules_version)

        b_spec = b_rules.validate_and_quantize_order(
            side=OrderSide.BUY,
            raw_qty=Decimal("0.1234567"),
            raw_price=Decimal("68000.129"),
            now_utc=now,
        )
        self.assertEqual(b_spec.quantized_qty, Decimal("0.12345"))
        self.assertEqual(b_spec.quantized_price, Decimal("68000.12"))

        o_spec = o_rules.validate_and_quantize_order(
            side=OrderSide.SELL,
            raw_qty=Decimal("3.8"),
            raw_price=Decimal("68000.01"),
            now_utc=now,
        )
        self.assertEqual(o_spec.quantized_qty, Decimal("3"))
        self.assertEqual(o_spec.effective_base_exposure, Decimal("0.03"))
        self.assertEqual(o_spec.quantized_price, Decimal("68000.1"))

        with self.assertRaises(RulesExpiredError):
            reg.get_rules("binance_spot", "BTC-USDT", now_utc=now + timedelta(seconds=601))

    def test_core006_ledger_cycle_audit_and_idempotent_fills(self) -> None:
        store = LedgerStore(":memory:")
        settings = Settings.from_env(RISK_YAML_PATH)
        cfg_hash = store.record_config_version(settings)

        now = datetime.now(timezone.utc)
        opp = Opportunity(
            opportunity_id="opp_test_001",
            strategy_id="strat_cross_cex_spot",
            strategy_version="1.0.0",
            status=OpportunityStatus.APPROVED,
            legs=[
                OpportunityLeg(
                    venue_id="binance_spot",
                    instrument_id="BTC-USDT",
                    side=OrderSide.BUY,
                    target_quantity=Decimal("0.01"),
                    executable_price=Decimal("68000"),
                    executable_price_type=PriceType.BEST_ASK,
                    available_depth_qty=Decimal("0.5"),
                    expected_fee=Decimal("0.34"),
                    fee_asset="USDT",
                    rules_version="rules_b1",
                ),
                OpportunityLeg(
                    venue_id="okx_spot",
                    instrument_id="BTC-USDT",
                    side=OrderSide.SELL,
                    target_quantity=Decimal("0.01"),
                    executable_price=Decimal("68100"),
                    executable_price_type=PriceType.BEST_BID,
                    available_depth_qty=Decimal("0.8"),
                    expected_fee=Decimal("0.34"),
                    fee_asset="USDT",
                    rules_version="rules_o1",
                ),
            ],
            input_snapshot_ids=["snap_1", "snap_2"],
            settlement_asset="USDT",
            gross_edge=Decimal("1.00"),
            fees=Decimal("0.68"),
            expected_net_pnl=Decimal("0.32"),
            net_edge_bps=Decimal("4.7"),
            capacity=Decimal("680"),
            expires_at=now + timedelta(seconds=2),
            decision=OpportunityDecision.PAPER,
        )
        store.record_opportunity(opp)

        plan = ExecutionPlan(
            cycle_id="cyc_test_001",
            opportunity_id=opp.opportunity_id,
            strategy_id=opp.strategy_id,
            strategy_version=opp.strategy_version,
            execution_mode="paper",
            state=CycleState.RESERVED,
            legs=[
                ExecutionPlanLeg(
                    client_order_id="cid_buy_1",
                    venue_id="binance_spot",
                    instrument_id="BTC-USDT",
                    side=OrderSide.BUY,
                    quantity=Decimal("0.01"),
                    limit_price=Decimal("68000"),
                    max_slippage_bps=Decimal("5"),
                    rules_version="rules_b1",
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
            strategy_id=opp.strategy_id,
            policy_version=settings.risk_policy.policy_version,
            approved=True,
            reason_codes=["PASS_ALL_GATES"],
        )
        store.create_cycle(plan, rd)

        oe_new = OrderEvent(
            cycle_id=plan.cycle_id,
            strategy_id=plan.strategy_id,
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            client_order_id="cid_buy_1",
            venue_order_id="b_ord_100",
            side=OrderSide.BUY,
            order_type=OrderType.IOC,
            status=OrderStatus.NEW,
            native_venue_status="NEW",
            order_quantity=Decimal("0.01"),
            limit_price=Decimal("68000"),
            cumulative_filled_qty=Decimal("0"),
        )
        store.record_order_event(oe_new)

        fill_1 = Fill(
            fill_id="fill_001",
            venue_trade_id="trd_9001",
            cycle_id=plan.cycle_id,
            strategy_id=plan.strategy_id,
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            client_order_id="cid_buy_1",
            venue_order_id="b_ord_100",
            side=OrderSide.BUY,
            fill_qty=Decimal("0.01"),
            fill_price=Decimal("68000"),
            fee_amount=Decimal("0.34"),
            fee_asset="USDT",
        )
        self.assertTrue(store.record_fill(fill_1))
        self.assertFalse(store.record_fill(fill_1))

        oe_regress = OrderEvent(
            cycle_id=plan.cycle_id,
            strategy_id=plan.strategy_id,
            venue_id="binance_spot",
            instrument_id="BTC-USDT",
            client_order_id="cid_buy_1",
            venue_order_id="b_ord_100",
            side=OrderSide.BUY,
            order_type=OrderType.IOC,
            status=OrderStatus.CANCELED,
            native_venue_status="CANCELED",
            order_quantity=Decimal("0.01"),
            cumulative_filled_qty=Decimal("0.005"),
        )
        with self.assertRaisesRegex(LedgerIntegrityError, "regression forbidden"):
            store.record_order_event(oe_regress)

        trail = store.get_cycle_audit_trail("cyc_test_001")
        self.assertEqual(trail.opportunity["opportunity_id"], "opp_test_001")
        self.assertEqual(len(trail.risk_decisions), 1)
        self.assertEqual(len(trail.orders), 1)
        self.assertEqual(trail.orders[0]["status"], OrderStatus.FILLED.value)
        self.assertEqual(len(trail.fills), 1)
        self.assertEqual(len(trail.fees), 1)

        self.assertFalse(store.is_kill_switch_active("GLOBAL"))
        store.set_kill_switch("GLOBAL", True, "Portfolio daily loss exceeded", "RiskGateway")
        self.assertTrue(store.is_kill_switch_active("GLOBAL"))

    def test_core007_observability_logging_metrics_and_alerts(self) -> None:
        secret_val = "BINANCE_PRIVATE_SIGN_KEY_ABCDEF12345"
        logger = StructuredLogger(known_secrets=[secret_val])
        metrics = MetricsRegistry()
        alerts = AlertManager(metrics=metrics, logger=logger)

        ctx = TraceContext(
            strategy_id="strat_triangle_spot",
            venue_id="binance_spot",
            cycle_id="cyc_777",
            client_order_id="cid_777",
        )

        raw_json = logger.log(
            "INFO",
            f"Submitting order with signature={secret_val}",
            ctx,
            api_secret=secret_val,
            latency_ms=12,
        )
        self.assertNotIn(secret_val, raw_json)
        parsed = json.loads(raw_json)
        for req_field in StructuredLogger.REQUIRED_FIELDS:
            self.assertIn(req_field, parsed)
            self.assertTrue(parsed[req_field])

        alert_ev = alerts.trigger(
            "UNKNOWN_ORDER_DETECTED",
            "Order cid_777 timed out after 3000ms; state=UNKNOWN",
            ctx,
        )
        self.assertEqual(alert_ev.severity, AlertSeverity.P0)
        self.assertIn("p0-unknown-order-reconciliation.md", alert_ev.runbook_url)
        self.assertEqual(
            metrics.get_counter(
                "arb_alerts_total",
                labels={"rule": "UNKNOWN_ORDER_DETECTED", "severity": "P0", "venue_id": "binance_spot"},
            ),
            1.0,
        )




from arb.marketdata import (
    HealthReasonCode,
    MarketHealthGate,
    OrderBookBuilder,
    SequenceStatus,
)
from arb.venues.binance_spot import (
    BinanceBookTickerEvent,
    BinanceDiffDepthEvent,
    BinanceSpotRestAdapter,
    BinanceSpotStreamManager,
    HttpResponsePayload,
    HttpTransport,
    StreamOverloadError,
)
from typing import Any, Callable, Dict



class MockHttpTransport(HttpTransport):
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
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "timeout_ms": timeout_ms})
        return self._handler(method, url, params, headers)


class TestSprint2MarketData(unittest.TestCase):
    # ==========================================================================
    # MD-001: Binance Spot REST Adapter Tests
    # ==========================================================================

    def test_md001_server_time_and_clock_skew_guard(self) -> None:
        now = datetime(2026, 10, 26, 2, 0, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        # 1. Normal server time within 1000ms skew
        transport_ok = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(status_code=200, json_body={"serverTime": now_ms - 120})
        )
        adapter_ok = BinanceSpotRestAdapter(transport=transport_ok, max_clock_skew_ms=1000)
        srv_dt = asyncio.run(adapter_ok.get_server_time(local_now_utc=now))
        self.assertEqual(int(srv_dt.timestamp() * 1000), now_ms - 120)

        # 2. Clock skew > 1000ms raises TRANSIENT_NETWORK with native_code -1021
        transport_skew = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(status_code=200, json_body={"serverTime": now_ms - 2500})
        )
        adapter_skew = BinanceSpotRestAdapter(transport=transport_skew, max_clock_skew_ms=1000)
        with self.assertRaises(ExchangeError) as ctx:
            asyncio.run(adapter_skew.get_server_time(local_now_utc=now))
        self.assertEqual(ctx.exception.category, ErrorCategory.TRANSIENT_NETWORK)
        self.assertEqual(ctx.exception.native_code, "-1021")

    def test_md001_exchange_info_and_depth_snapshot_parsing(self) -> None:
        exchange_info_payload = {
            "timezone": "UTC",
            "serverTime": 1792980000000,
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": True,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                        {"filterType": "NOTIONAL", "minNotional": "10.00000000"},
                    ],
                },
                {
                    "symbol": "LUNAUSDT",
                    "status": "BREAK",
                    "baseAsset": "LUNA",
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": False,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
                    ],
                },
            ],
        }
        depth_payload = {
            "lastUpdateId": 1050,
            "bids": [["67990.50", "1.25000"], ["68000.00", "0.50000"], ["67980.00", "0.00000"]],
            "asks": [["68005.00", "2.00000"], ["68001.00", "0.80000"]],
        }

        def router(method: str, url: str, params: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> HttpResponsePayload:
            if url.endswith("/api/v3/exchangeInfo"):
                return HttpResponsePayload(status_code=200, json_body=exchange_info_payload)
            if url.endswith("/api/v3/depth"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body=depth_payload,
                    headers={"X-MBX-USED-WEIGHT-1M": "42"},
                )
            raise AssertionError(f"Unexpected URL: {url}")

        metrics = MetricsRegistry()
        adapter = BinanceSpotRestAdapter(transport=MockHttpTransport(router), metrics=metrics)
        instruments = asyncio.run(adapter.fetch_instruments())
        self.assertEqual(len(instruments), 2)
        btc = instruments[0]
        self.assertEqual(btc.instrument_id, "BTC-USDT")
        self.assertEqual(btc.venue_symbol, "BTCUSDT")
        self.assertEqual(btc.tick_size, Decimal("0.01"))
        self.assertEqual(btc.step_size, Decimal("0.00001"))
        self.assertEqual(btc.min_notional, Decimal("10.0"))
        self.assertEqual(btc.trading_status, TradingStatus.TRADING)

        luna = instruments[1]
        self.assertEqual(luna.trading_status, TradingStatus.HALTED)

        book = asyncio.run(adapter.fetch_order_book("BTC-USDT", depth=20))
        self.assertEqual(book.last_update_id, 1050)
        # Zero-qty level filtered out, bids sorted descending, asks sorted ascending
        self.assertEqual(len(book.bids), 2)
        self.assertEqual(book.bids[0].price, Decimal("68000.00"))
        self.assertEqual(book.asks[0].price, Decimal("68001.00"))
        self.assertEqual(
            metrics.get_gauge("arb_binance_used_weight_1m", labels={"venue_id": "binance_spot"}),
            42.0,
        )

    def test_md001_http_error_classification_429_418_5xx_timeout_and_readonly_gate(self) -> None:
        metrics = MetricsRegistry()
        logger = StructuredLogger()
        alerts = AlertManager(metrics=metrics, logger=logger)

        # 1. HTTP 429 sets rate limit backoff and fires P1 alert
        t_429 = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(
                status_code=429,
                json_body={"code": -1003, "msg": "Too many requests; current limit is 6000 weight per minute"},
                headers={"Retry-After": "10"},
            )
        )
        ad_429 = BinanceSpotRestAdapter(transport=t_429, metrics=metrics, logger=logger, alerts=alerts)
        with self.assertRaises(ExchangeError) as ctx429:
            asyncio.run(ad_429.fetch_order_book("BTC-USDT"))
        self.assertEqual(ctx429.exception.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(ctx429.exception.retry_after_ms, 10000)
        self.assertTrue(ad_429.rate_limit_backoff_until_ms > 0)

        # 2. HTTP 418 locks the adapter (IP ban) and blocks subsequent requests immediately
        t_418 = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(
                status_code=418,
                json_body={"code": -1003, "msg": "IP banned until 1792989999000"},
                headers={"Retry-After": "120"},
            )
        )
        ad_418 = BinanceSpotRestAdapter(transport=t_418, metrics=metrics, logger=logger, alerts=alerts)
        with self.assertRaises(ExchangeError) as ctx418:
            asyncio.run(ad_418.fetch_order_book("BTC-USDT"))
        self.assertEqual(ctx418.exception.category, ErrorCategory.IP_BANNED)
        self.assertTrue(ad_418.is_ip_banned)
        # Subsequent call fails without hitting transport
        with self.assertRaises(ExchangeError) as ctx_locked:
            asyncio.run(ad_418.get_server_time())
        self.assertEqual(ctx_locked.exception.category, ErrorCategory.IP_BANNED)
        self.assertEqual(len(t_418.calls), 1)

        # 3. Phase 1 Read-Only Mode blocks submit_order by default
        with self.assertRaises(ExchangeError) as ctx_ro:
            asyncio.run(
                ad_429.submit_order(
                    cycle_id="cyc_1",
                    strategy_id="strat_1",
                    instrument_id="BTC-USDT",
                    client_order_id="cid_1",
                    side=OrderSide.BUY,
                    order_type=OrderType.IOC,
                    quantity=Decimal("0.01"),
                    limit_price=Decimal("68000"),
                )
            )
        self.assertEqual(ctx_ro.exception.category, ErrorCategory.AUTH_PERMISSION)

        # 4. When unlocked for write, TimeoutError or HTTP 504 returns OrderStatus.UNKNOWN (never definite failure!)
        def timeout_handler(m: str, u: str, p: Any, h: Any) -> HttpResponsePayload:
            raise TimeoutError("Read timed out")

        creds = VenueCredentials(
            venue_id="binance_spot",
            api_key=SecretStr("k_test_12345"),
            api_secret=SecretStr("s_test_67890"),
        )
        ad_write = BinanceSpotRestAdapter(
            transport=MockHttpTransport(timeout_handler),
            credentials=creds,
            read_only_mode=False,
        )
        unknown_ev = asyncio.run(
            ad_write.submit_order(
                cycle_id="cyc_99",
                strategy_id="strat_s1",
                instrument_id="BTC-USDT",
                client_order_id="cid_timeout_99",
                side=OrderSide.BUY,
                order_type=OrderType.IOC,
                quantity=Decimal("0.05"),
                limit_price=Decimal("68000"),
            )
        )
        self.assertEqual(unknown_ev.status, OrderStatus.UNKNOWN)
        self.assertIn("UNKNOWN_ON_", unknown_ev.native_venue_status)

    # ==========================================================================
    # MD-002 & MD-003: WebSocket Collector & Official L2 OrderBook Sync Tests
    # ==========================================================================

    def test_md003_official_orderbook_snapshot_and_diff_sync_lifecycle(self) -> None:
        builder = OrderBookBuilder(venue_id="binance_spot", instrument_id="BTC-USDT")
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.UNINITIALIZED)

        now = datetime(2026, 10, 26, 2, 10, 0, tzinfo=timezone.utc)

        # Step 1: Buffer WebSocket diffs before REST snapshot arrives
        # Packet 1: U=998, u=1000 (will be dropped since snapshot lastUpdateId=1000)
        builder.buffer_or_apply_diff(
            first_update_id=998,
            final_update_id=1000,
            bid_deltas=[PriceLevel(price=Decimal("67900"), quantity=Decimal("9.9"))],
            ask_deltas=[],
            event_time_utc=now,
        )
        # Packet 2: U=1000, u=1002 (spans snapshot+1=1001: U <= 1001 <= u -> MUST be applied!)
        builder.buffer_or_apply_diff(
            first_update_id=1000,
            final_update_id=1002,
            bid_deltas=[PriceLevel(price=Decimal("68000"), quantity=Decimal("2.5"))],
            ask_deltas=[PriceLevel(price=Decimal("68010"), quantity=Decimal("0"))],  # deletes 68010 ask!
            event_time_utc=now,
        )
        # Packet 3: U=1003, u=1004 (continuous -> applied!)
        builder.buffer_or_apply_diff(
            first_update_id=1003,
            final_update_id=1004,
            bid_deltas=[],
            ask_deltas=[PriceLevel(price=Decimal("68015"), quantity=Decimal("1.5"))],
            event_time_utc=now,
        )
        self.assertEqual(builder.sequence_status, SequenceStatus.BUFFERING)

        # Step 2: Apply REST snapshot with lastUpdateId = 1000
        ok = builder.apply_snapshot(
            bids=[
                PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0")),
                PriceLevel(price=Decimal("67990"), quantity=Decimal("3.0")),
            ],
            asks=[
                PriceLevel(price=Decimal("68010"), quantity=Decimal("0.8")),
                PriceLevel(price=Decimal("68020"), quantity=Decimal("2.0")),
            ],
            last_update_id=1000,
            event_time_utc=now,
        )
        self.assertTrue(ok)
        self.assertTrue(builder.is_valid())
        self.assertEqual(builder.last_update_id, 1004)

        # Verify buffered replay results:
        # - Packet 1 (u=1000) was dropped (67900 not in book)
        # - Packet 2 updated 68000 bid to 2.5 and deleted 68010 ask
        # - Packet 3 inserted 68015 ask with 1.5
        book = builder.to_order_book()
        self.assertEqual(book.bids[0].price, Decimal("68000"))
        self.assertEqual(book.bids[0].quantity, Decimal("2.5"))
        self.assertEqual(book.asks[0].price, Decimal("68015"))
        self.assertEqual(book.asks[0].quantity, Decimal("1.5"))
        self.assertEqual(book.asks[1].price, Decimal("68020"))

        # Step 3: Duplicate/old event (u=1004 <= last_update_id=1004) is safely ignored
        self.assertTrue(
            builder.apply_diff(
                first_update_id=1003,
                final_update_id=1004,
                bid_deltas=[PriceLevel(price=Decimal("68000"), quantity=Decimal("999"))],
                ask_deltas=[],
                event_time_utc=now,
            )
        )
        self.assertEqual(builder.to_order_book().bids[0].quantity, Decimal("2.5"))

        # Step 4: Simulate depth walk across multiple levels (BUY 2.5 BTC walks 1.5 @ 68015 + 1.0 @ 68020)
        slice_res = builder.depth(OrderSide.BUY, Decimal("2.5"))
        self.assertTrue(slice_res.is_fully_filled)
        self.assertEqual(slice_res.filled_qty, Decimal("2.5"))
        self.assertEqual(slice_res.levels_consumed, 2)
        expected_quote = Decimal("1.5") * Decimal("68015") + Decimal("1.0") * Decimal("68020")
        self.assertEqual(slice_res.total_quote_notional, expected_quote)

        # Step 5: Any sequence gap (e.g. U=1006 when last_update_id=1004, missing 1005) IMMEDIATELY invalidates book (CORE-001 P0 test)
        gap_ok = builder.apply_diff(
            first_update_id=1006,
            final_update_id=1007,
            bid_deltas=[PriceLevel(price=Decimal("68001"), quantity=Decimal("1.0"))],
            ask_deltas=[],
            event_time_utc=now,
        )
        self.assertFalse(gap_ok)
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.GAP_DETECTED)
        with self.assertRaisesRegex(RuntimeError, "Cannot compute depth on invalid order book"):
            builder.depth(OrderSide.BUY, Decimal("0.1"))

    def test_md002_websocket_stream_disconnect_reconnect_rotation_and_overload(self) -> None:
        builder = OrderBookBuilder(venue_id="binance_spot", instrument_id="BTC-USDT")
        now = datetime(2026, 10, 26, 2, 20, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        metrics = MetricsRegistry()
        logger = StructuredLogger()
        alerts = AlertManager(metrics=metrics, logger=logger)

        mgr = BinanceSpotStreamManager(
            symbol_to_instrument_id={"BTCUSDT": "BTC-USDT"},
            order_books={"BTC-USDT": builder},
            max_queue_size=3,
            rotation_interval_seconds=23 * 3600,
            metrics=metrics,
            logger=logger,
            alerts=alerts,
        )

        sub = mgr.build_subscription_payload(request_id=7, depth_interval_ms=100)
        self.assertEqual(sub["params"], ["btcusdt@bookTicker", "btcusdt@depth@100ms"])

        mgr.on_connect(now_utc=now)
        self.assertTrue(mgr.is_connected)
        # Apply snapshot so book becomes valid
        builder.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68010"), quantity=Decimal("1.0"))],
            last_update_id=100,
            event_time_utc=now,
        )
        self.assertTrue(builder.is_valid())

        # Send valid diff depth message via WebSocket stream manager
        diff_msg = {
            "e": "depthUpdate",
            "E": now_ms + 50,
            "s": "BTCUSDT",
            "U": 101,
            "u": 102,
            "b": [["68002.00", "1.50"]],
            "a": [["68010.00", "0.80"]],
        }
        ev1 = mgr.on_raw_message(diff_msg, receive_time_utc=now + timedelta(milliseconds=55))
        self.assertIsInstance(ev1, BinanceDiffDepthEvent)
        self.assertEqual(builder.last_update_id, 102)
        self.assertEqual(builder.to_order_book().bids[0].price, Decimal("68002.00"))

        # Send bookTicker message
        bbo_msg = {
            "u": 103,
            "s": "BTCUSDT",
            "b": "68002.00",
            "B": "1.50",
            "a": "68010.00",
            "A": "0.80",
            "E": now_ms + 60,
        }
        ev2 = mgr.on_raw_message(bbo_msg, receive_time_utc=now + timedelta(milliseconds=65))
        self.assertIsInstance(ev2, BinanceBookTickerEvent)

        # Proactive 23h connection rotation check
        self.assertFalse(mgr.needs_proactive_rotation(now_utc=now + timedelta(hours=22)))
        self.assertTrue(mgr.needs_proactive_rotation(now_utc=now + timedelta(hours=23, seconds=1)))

        # Disconnect immediately invalidates book and fires alert
        mgr.on_disconnect(reason="NETWORK_DROP")
        self.assertFalse(mgr.is_connected)
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.DISCONNECTED)

        # Reconnect MUST NOT reuse old book (sets status to BUFFERING until fresh snapshot)
        mgr.on_connect(now_utc=now + timedelta(seconds=5))
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.BUFFERING)

        # Queue overload check (max_queue_size=3; currently 2 items in queue -> 3rd item succeeds, 4th triggers circuit breaker)
        mgr.on_raw_message(bbo_msg, receive_time_utc=now + timedelta(seconds=6))
        with self.assertRaises(StreamOverloadError):
            mgr.on_raw_message(bbo_msg, receive_time_utc=now + timedelta(seconds=7))
        self.assertTrue(mgr.is_circuit_broken)
        self.assertFalse(builder.is_valid())

    # ==========================================================================
    # MD-004: Unified MarketHealthGate Tests (CORE-002 P0 Test)
    # ==========================================================================

    def test_md004_market_health_freshness_and_cross_venue_skew_gates(self) -> None:
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)  # max_book_age_ms=500, max_cross_venue_skew_ms=250, max_ws_queue_lag_ms=100
        gate = MarketHealthGate(risk_policy=policy)

        t0 = datetime(2026, 10, 26, 2, 30, 0, tzinfo=timezone.utc)

        b_book = OrderBookBuilder("binance_spot", "BTC-USDT")
        b_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68010"), quantity=Decimal("1.0"))],
            last_update_id=500,
            event_time_utc=t0,
            receive_time_utc=t0,
        )

        o_book = OrderBookBuilder("okx_spot", "BTC-USDT")
        o_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68005"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68015"), quantity=Decimal("1.0"))],
            last_update_id=900,
            event_time_utc=t0 + timedelta(milliseconds=100),
            receive_time_utc=t0 + timedelta(milliseconds=100),
        )

        # 1. Both books fresh (age 150ms & 50ms <= 500ms) and skew 100ms <= 250ms -> HEALTHY
        ok, reports, reasons = gate.evaluate_multi_book_gate(
            [b_book, o_book],
            now_utc=t0 + timedelta(milliseconds=150),
        )
        self.assertTrue(ok)
        self.assertEqual(len(reasons), 0)
        self.assertEqual(reports[0].cross_venue_skew_ms, 100)

        # 2. Stale book age (now = t0 + 600ms -> b_book age is 600ms > 500ms) -> REJECTED with STALE_BOOK_AGE
        ok_stale, reports_stale, reasons_stale = gate.evaluate_multi_book_gate(
            [b_book, o_book],
            now_utc=t0 + timedelta(milliseconds=600),
        )
        self.assertFalse(ok_stale)
        self.assertIn("binance_spot:BTC-USDT:STALE_BOOK_AGE", reasons_stale)
        self.assertEqual(reports_stale[0].status, DataHealthStatus.STALE)

        # 3. Cross-venue skew > 250ms (even if both books are < 500ms old) -> REJECTED with CROSS_VENUE_SKEW_EXCEEDED
        o_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68005"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68015"), quantity=Decimal("1.0"))],
            last_update_id=901,
            event_time_utc=t0 + timedelta(milliseconds=300),
            receive_time_utc=t0 + timedelta(milliseconds=300),
        )
        ok_skew, reports_skew, reasons_skew = gate.evaluate_multi_book_gate(
            [b_book, o_book],
            now_utc=t0 + timedelta(milliseconds=350),
        )
        self.assertFalse(ok_skew)
        self.assertIn("binance_spot:BTC-USDT:CROSS_VENUE_SKEW_EXCEEDED", reasons_skew)
        self.assertEqual(reports_skew[0].cross_venue_skew_ms, 300)

        # 4. WebSocket queue lag > 100ms -> REJECTED with QUEUE_LAG_EXCEEDED
        h_lag = gate.evaluate_book(
            o_book,
            now_utc=t0 + timedelta(milliseconds=350),
            queue_lag_ms=150,
        )
        self.assertFalse(h_lag.validity)
        self.assertIn(HealthReasonCode.QUEUE_LAG_EXCEEDED.value, h_lag.reason_codes)




import tempfile
from typing import Tuple
from arb.costs import (
    CostEngine,
    FeeSchedule,
    ReferenceCostCalculator,
)
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




from arb.strategies.cross_cex_spot import (
    CrossCexPairMapping,
    CrossCexSpotScanner,
    FxRateQuote,
    VenueTransferStatus,
)
from arb.venues.okx_spot import (
    OkxOrderBookEvent,
    OkxSpotRestAdapter,
    OkxSpotStreamManager,
)



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


# ============================================================================
# SPRINT 5: RISK GATEWAY, ORDER FSM, RECONCILIATION, PAPER ENGINE & FAULT DRILLS
# ============================================================================
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
        self.repo_root = Path("/working_dir/c_8b6fc0dea8cfd9c3/arb-platform")
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


# ============================================================================
# SPRINT 6: DERIVATIVE ADAPTERS, S2-A BASIS, S2-B FUNDING & MARGIN MONITOR
# ============================================================================
"""[DERIV-004] Sprint 6 Unit, Contract & S2 Acceptance Matrix Tests (`DERIV-001` ~ `DERIV-004`, `S2-001` ~ `S2-012`)."""

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
    unittest.main()
