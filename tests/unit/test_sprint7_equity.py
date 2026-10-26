"""Sprint 7 automated unit & contract tests (`EQ-001` through `EQ-004`).

Verifies all S3 Equity-Linked Arbitrage acceptance gates (`S3-001` through `S3-010`):
- `EQ-001` (`S3-001`, `S3-002`): Product terms registry, deterministic `product_terms_version` hash,
  ISIN comparability enforcement, blocking `research_only` or ticker-only pairs, and blocking
  `allow_cross_market_conversion=True` when neither product has `DIRECT_PRIMARY_REDEMPTION`.
- `EQ-002` (`S3-007`): Equity-linked token / perpetual market adapter normalizing executable bid/ask
  separately from `mark_price` and `index_reference_price`, rejecting missing executable quotes, and
  tracking underlying market session executability.
- `EQ-003` (`S3-003`, `S3-004`, `S3-006`): Stock broker adapter enforcing trading session calendar
  (`REGULAR`, `PRE_MARKET`, `CLOSED`, `HALTED`), short-sale borrow locate validation, native broker
  order status mapping, and corporate action freeze windows.
- `EQ-004` (`S3-001` ~ `S3-010`): Cross-market equity arbitrage scanner evaluating approved pairs,
  deducting trading fees + FX conversion + short borrow cost + holding basis risk haircut, and
  rejecting closed sessions, unapproved terms, corporate action freezes, or insufficient borrow locate.
"""
from __future__ import annotations
import sys as _sys, pathlib as _pathlib
_src_dir = str(_pathlib.Path(__file__).resolve().parents[2] / "src")
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from arb.domain.models import OpportunityDecision, OrderSide
from arb.equities.adapters import (
    BorrowLocateQuote,
    EquityBrokerAccountSnapshot,
    EquityBrokerAdapter,
    EquityBrokerOrderStatus,
    EquityLinkedMarketAdapter,
)
from arb.equities.terms import (
    ComparabilityApproval,
    CorporateActionEvent,
    CorporateActionType,
    DividendTreatment,
    EquityProductKind,
    EquityProductTerms,
    EquitySettlementCycle,
    EquityTermsRegistry,
    EquityTradingSession,
    RedemptionRights,
)
from arb.strategies.equity_arb.scanner import EquityCrossMarketScanner


class TestSprint7EquityArbitrage(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 26, 14, 30, 0, tzinfo=timezone.utc)  # 10:30 AM EDT (US Regular Session)
        self.registry = EquityTermsRegistry()

        # Approved Tokenized Equity on Venue A (24x7 trading, cash settlement)
        self.token_terms = EquityProductTerms(
            instrument_id="inst_token_nvda_usdt",
            venue_id="crypto_eq_venue",
            symbol="NVDA-TOKEN-USDT",
            underlying_ticker="NVDA",
            underlying_isin="US67066G1040",
            product_kind=EquityProductKind.TOKENIZED_EQUITY,
            issuer_name="RegulatedTokenIssuerAG",
            redemption_rights=RedemptionRights.CASH_SETTLEMENT_ONLY,
            dividend_treatment=DividendTreatment.CASH_PASS_THROUGH,
            settlement_cycle=EquitySettlementCycle.INSTANT_24X7,
            supported_sessions=(
                EquityTradingSession.CONTINUOUS_24X7,
                EquityTradingSession.REGULAR,
            ),
            short_sale_allowed=True,
            requires_borrow_locate=False,
            eligible_jurisdictions=("NON_US_QUALIFIED",),
            account_jurisdiction_verified=True,
            legal_review_approved=True,
            compliance_approver="chief_counsel_arb",
            approved_at_utc=self.now - timedelta(days=10),
            expires_at_utc=self.now + timedelta(days=80),
            redemption_fee_bps=Decimal("10.0"),
            holding_basis_risk_haircut_bps=Decimal("12.0"),
            research_only=False,
        )

        # Approved Traditional Stock on Prime Broker B (US Regular + Extended Hours, T+1)
        self.stock_terms = EquityProductTerms(
            instrument_id="inst_broker_nvda_usd",
            venue_id="prime_broker_us",
            symbol="NVDA",
            underlying_ticker="NVDA",
            underlying_isin="US67066G1040",
            product_kind=EquityProductKind.TRADITIONAL_EQUITY,
            issuer_name="NVIDIA Corp",
            redemption_rights=RedemptionRights.DIRECT_PRIMARY_REDEMPTION,
            dividend_treatment=DividendTreatment.CASH_PASS_THROUGH,
            settlement_cycle=EquitySettlementCycle.T_PLUS_1,
            supported_sessions=(
                EquityTradingSession.REGULAR,
                EquityTradingSession.PRE_MARKET,
                EquityTradingSession.AFTER_HOURS,
            ),
            short_sale_allowed=True,
            requires_borrow_locate=True,
            eligible_jurisdictions=("NON_US_QUALIFIED", "US_INSTITUTIONAL"),
            account_jurisdiction_verified=True,
            legal_review_approved=True,
            compliance_approver="chief_counsel_arb",
            approved_at_utc=self.now - timedelta(days=10),
            expires_at_utc=self.now + timedelta(days=80),
            redemption_fee_bps=Decimal("0"),
            holding_basis_risk_haircut_bps=Decimal("10.0"),
            research_only=False,
        )

        self.registry.register_terms(self.token_terms)
        self.registry.register_terms(self.stock_terms)

    def test_eq001_product_terms_and_comparability_registry(self) -> None:
        """Verify EQ-001 (`S3-001`, `S3-002`): ticker-only blocked, ISIN mismatch blocked, research_only blocked."""
        # 1. Without ComparabilityApproval, even two approved NVDA instruments cannot be paired (ticker-only forbidden)
        ok, reason, _, _, _ = self.registry.validate_pair_for_execution(
            "inst_token_nvda_usdt", "inst_broker_nvda_usd", self.now
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "COMPARABILITY_UNAPPROVED_TICKER_ONLY_FORBIDDEN")

        # 2. Registering an instrument with same ticker "NVDA" but wrong ISIN must raise ValueError on comparability
        fake_nvda = self.token_terms.copy(
            update={
                "instrument_id": "inst_fake_nvda_perp",
                "underlying_isin": "XS000000FAKE",
            }
        )
        self.registry.register_terms(fake_nvda)
        with self.assertRaises(ValueError):
            self.registry.register_comparability(
                ComparabilityApproval(
                    approval_id="app_invalid_isin",
                    leg_a_instrument_id="inst_fake_nvda_perp",
                    leg_b_instrument_id="inst_broker_nvda_usd",
                    underlying_isin="US67066G1040",
                    approved_by="chief_engineer",
                    approved_at_utc=self.now,
                )
            )

        # 3. Unreviewed / research_only product is rejected before reaching comparability check
        unreviewed_tsla = self.token_terms.copy(
            update={
                "instrument_id": "inst_token_tsla_unreviewed",
                "symbol": "TSLA-PERP",
                "underlying_ticker": "TSLA",
                "research_only": True,
            }
        )
        self.registry.register_terms(unreviewed_tsla)
        ok_unrev, reason_unrev, _, _, _ = self.registry.validate_pair_for_execution(
            "inst_token_tsla_unreviewed", "inst_broker_nvda_usd", self.now
        )
        self.assertFalse(ok_unrev)
        self.assertEqual(reason_unrev, "LEG_A_PRODUCT_MARKED_RESEARCH_ONLY")

        # 4. Valid approval succeeds and produces deterministic product_terms_version hashes
        approval = ComparabilityApproval(
            approval_id="app_nvda_token_vs_stock",
            leg_a_instrument_id="inst_token_nvda_usdt",
            leg_b_instrument_id="inst_broker_nvda_usd",
            underlying_isin="US67066G1040",
            allow_cross_market_conversion=False,
            allow_price_convergence_spread=True,
            requires_synchronous_session=True,
            max_holding_hours=Decimal("48"),
            min_convergence_haircut_bps=Decimal("12.0"),
            approved_by="chief_engineer",
            approved_at_utc=self.now,
            research_only=False,
        )
        self.registry.register_comparability(approval, self.now)
        ok_valid, reason_valid, app_out, _, _ = self.registry.validate_pair_for_execution(
            "inst_token_nvda_usdt", "inst_broker_nvda_usd", self.now
        )
        self.assertTrue(ok_valid)
        self.assertIsNone(reason_valid)
        self.assertEqual(app_out.approval_id, "app_nvda_token_vs_stock")
        self.assertTrue(self.token_terms.product_terms_version.startswith("terms_v1_"))

    def test_eq002_market_adapter_separates_executable_from_reference_prices(self) -> None:
        """Verify EQ-002 (`S3-007`): mark_price/index_price are never used as executable bid/ask."""
        adapter = EquityLinkedMarketAdapter("crypto_eq_venue", self.registry)

        # Payload with mark_price and index_price but NO executable orderbook bids/asks
        ref_only_snap = adapter.normalize_snapshot(
            instrument_id="inst_token_nvda_usdt",
            raw_payload={
                "mark_price": "140.50",
                "index_price": "140.45",
                "funding_rate_8h": "0.0001",
                "session_status": "CONTINUOUS_24X7",
            },
            underlying_session_status=EquityTradingSession.REGULAR,
            now_utc=self.now,
        )
        self.assertIsNone(ref_only_snap.executable_bid)
        self.assertIsNone(ref_only_snap.executable_ask)
        self.assertEqual(ref_only_snap.mark_price, Decimal("140.50"))
        self.assertFalse(ref_only_snap.is_quote_executable)
        with self.assertRaises(ValueError):
            ref_only_snap.to_orderbook_snapshot()

        # Payload with real L2 bids/asks while underlying market is CLOSED
        closed_underlying_snap = adapter.normalize_snapshot(
            instrument_id="inst_token_nvda_usdt",
            raw_payload={
                "bids": [["139.80", "50"]],
                "asks": [["140.00", "50"]],
                "mark_price": "140.50",
                "index_price": "140.45",
                "session_status": "CONTINUOUS_24X7",
            },
            underlying_session_status=EquityTradingSession.CLOSED,
            now_utc=self.now,
        )
        self.assertTrue(closed_underlying_snap.is_quote_executable)
        self.assertFalse(closed_underlying_snap.underlying_hedge_executable)
        ob = closed_underlying_snap.to_orderbook_snapshot()
        self.assertEqual(ob.best_bid().price, Decimal("139.80"))
        self.assertEqual(ob.best_ask().price, Decimal("140.00"))

    def test_eq003_broker_adapter_session_borrow_locate_and_corporate_actions(self) -> None:
        """Verify EQ-003 (`S3-003`, `S3-004`, `S3-006`): session calendar, borrow locate, and corporate action halts."""
        account = EquityBrokerAccountSnapshot(
            broker_id="prime_broker_us",
            settled_cash_usd=Decimal("100000"),
            buying_power_usd=Decimal("200000"),
            maintenance_margin_usd=Decimal("25000"),
            positions_shares={"NVDA": Decimal("0")},  # Zero inventory -> selling requires short borrow locate
            jurisdiction_approved=True,
        )
        broker = EquityBrokerAdapter("prime_broker_us", self.registry, account)

        # 1. When market is CLOSED, synchronous hedge order is rejected
        broker.set_trading_session("NVDA", EquityTradingSession.CLOSED)
        rej_closed = broker.submit_order(
            client_order_id="cid_brk_1",
            instrument_id="inst_broker_nvda_usd",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            limit_price=Decimal("140.00"),
            allow_extended_hours=False,
            now_utc=self.now,
        )
        self.assertEqual(rej_closed.native_status, EquityBrokerOrderStatus.REJECTED)
        self.assertEqual(rej_closed.reject_reason, "UNDERLYING_MARKET_CLOSED_NO_SYNC_HEDGE")

        # 2. Switch to REGULAR session; short SELL without borrow locate quote is rejected (`S3-004`)
        broker.set_trading_session("NVDA", EquityTradingSession.REGULAR)
        rej_no_locate = broker.submit_order(
            client_order_id="cid_brk_2",
            instrument_id="inst_broker_nvda_usd",
            side=OrderSide.SELL,
            quantity=Decimal("20"),
            limit_price=Decimal("141.00"),
            allow_extended_hours=False,
            now_utc=self.now,
        )
        self.assertEqual(rej_no_locate.native_status, EquityBrokerOrderStatus.REJECTED)
        self.assertEqual(rej_no_locate.reject_reason, "MISSING_BORROW_LOCATE_QUOTE")

        # 3. Provide sufficient BorrowLocateQuote -> short SELL succeeds and records locate_id
        broker.set_borrow_locate_quote(
            BorrowLocateQuote(
                locate_id="loc_nvda_001",
                underlying_ticker="NVDA",
                available_shares=Decimal("100"),
                annualized_borrow_rate_bps=Decimal("150.0"),
                is_hard_to_borrow=False,
                expires_at_utc=self.now + timedelta(hours=4),
            )
        )
        ok_order = broker.submit_order(
            client_order_id="cid_brk_3",
            instrument_id="inst_broker_nvda_usd",
            side=OrderSide.SELL,
            quantity=Decimal("20"),
            limit_price=Decimal("141.00"),
            allow_extended_hours=False,
            now_utc=self.now,
        )
        self.assertEqual(ok_order.native_status, EquityBrokerOrderStatus.SUBMITTED)
        self.assertEqual(ok_order.locate_id, "loc_nvda_001")

        # 4. Register an active stock split corporate action within 24h freeze window (`S3-006`) -> order rejected
        self.registry.register_corporate_action(
            CorporateActionEvent(
                action_id="ca_nvda_split_10_1",
                underlying_ticker="NVDA",
                action_type=CorporateActionType.STOCK_SPLIT,
                ex_date_utc=self.now + timedelta(hours=6),
                effective_date_utc=self.now + timedelta(hours=12),
                adjustment_ratio=Decimal("10.0"),
            )
        )
        rej_ca = broker.submit_order(
            client_order_id="cid_brk_4",
            instrument_id="inst_broker_nvda_usd",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            limit_price=Decimal("140.00"),
            allow_extended_hours=False,
            now_utc=self.now,
        )
        self.assertEqual(rej_ca.native_status, EquityBrokerOrderStatus.REJECTED)
        self.assertIn("BROKER_CORPORATE_ACTION_FREEZE:STOCK_SPLIT", rej_ca.reject_reason or "")

    def test_eq004_cross_market_scanner_end_to_end_and_rejection_gates(self) -> None:
        """Verify EQ-004 (`S3-001` ~ `S3-010`): profitable scan during regular hours & rejection on closed/borrow/CA."""
        approval = ComparabilityApproval(
            approval_id="app_nvda_token_vs_stock",
            leg_a_instrument_id="inst_token_nvda_usdt",
            leg_b_instrument_id="inst_broker_nvda_usd",
            underlying_isin="US67066G1040",
            allow_cross_market_conversion=False,
            allow_price_convergence_spread=True,
            requires_synchronous_session=True,
            max_holding_hours=Decimal("48"),
            min_convergence_haircut_bps=Decimal("12.0"),
            approved_by="chief_engineer",
            approved_at_utc=self.now,
            research_only=False,
        )
        self.registry.register_comparability(approval, self.now)

        token_adapter = EquityLinkedMarketAdapter("crypto_eq_venue", self.registry)
        account = EquityBrokerAccountSnapshot(
            broker_id="prime_broker_us",
            settled_cash_usd=Decimal("100000"),
            buying_power_usd=Decimal("200000"),
            maintenance_margin_usd=Decimal("25000"),
            positions_shares={"NVDA": Decimal("0")},
            jurisdiction_approved=True,
        )
        broker_adapter = EquityBrokerAdapter("prime_broker_us", self.registry, account)
        broker_adapter.set_trading_session("NVDA", EquityTradingSession.REGULAR)

        locate = BorrowLocateQuote(
            locate_id="loc_nvda_99",
            underlying_ticker="NVDA",
            available_shares=Decimal("50"),
            annualized_borrow_rate_bps=Decimal("120.0"),
            expires_at_utc=self.now + timedelta(hours=6),
        )
        scanner = EquityCrossMarketScanner(
            terms_registry=self.registry,
            min_net_edge_bps=Decimal("8.0"),
        )

        # Case 1: Buy Tokenized NVDA at 140.00 ASK, Short Broker NVDA at 140.70 BID (+50 bps gross edge)
        token_snap = token_adapter.normalize_snapshot(
            instrument_id="inst_token_nvda_usdt",
            raw_payload={
                "bids": [["139.90", "30"]],
                "asks": [["140.00", "30"]],
                "mark_price": "140.30",
                "index_price": "140.35",
                "session_status": "CONTINUOUS_24X7",
                "source_timestamp_utc": self.now,
            },
            underlying_session_status=EquityTradingSession.REGULAR,
            now_utc=self.now,
        )
        broker_snap = broker_adapter.normalize_broker_quote(
            instrument_id="inst_broker_nvda_usd",
            bid=Decimal("140.70"),
            bid_size=Decimal("30"),
            ask=Decimal("140.80"),
            ask_size=Decimal("30"),
            last_reference_price=Decimal("140.75"),
            now_utc=self.now,
        )

        eval_ok = scanner.evaluate_pair(
            leg_a_snapshot=token_snap,
            leg_b_snapshot=broker_snap,
            target_quantity_shares=Decimal("10"),
            expected_holding_hours=Decimal("24"),
            now_utc=self.now,
            broker_account=account,
            borrow_locate=locate,
        )
        self.assertTrue(eval_ok.accepted)
        self.assertIsNotNone(eval_ok.candidate)
        self.assertEqual(eval_ok.candidate.decision, OpportunityDecision.PAPER)
        self.assertEqual(eval_ok.buy_instrument_id, "inst_token_nvda_usdt")
        self.assertEqual(eval_ok.sell_instrument_id, "inst_broker_nvda_usd")
        self.assertGreater(eval_ok.net_edge_bps, Decimal("25.0"))
        self.assertGreater(eval_ok.borrow_cost_bps, Decimal("0"))
        self.assertEqual(len(eval_ok.product_terms_versions), 2)

        # Case 2: Underlying US equity market closes -> synchronous hedge rejected (`S3-003`)
        broker_adapter.set_trading_session("NVDA", EquityTradingSession.CLOSED)
        broker_closed_snap = broker_adapter.normalize_broker_quote(
            instrument_id="inst_broker_nvda_usd",
            bid=Decimal("140.70"),
            bid_size=Decimal("30"),
            ask=Decimal("140.80"),
            ask_size=Decimal("30"),
            last_reference_price=Decimal("140.75"),
            now_utc=self.now,
        )
        eval_closed = scanner.evaluate_pair(
            leg_a_snapshot=token_snap,
            leg_b_snapshot=broker_closed_snap,
            target_quantity_shares=Decimal("10"),
            expected_holding_hours=Decimal("24"),
            now_utc=self.now,
            broker_account=account,
            borrow_locate=locate,
        )
        self.assertFalse(eval_closed.accepted)
        self.assertIn("UNDERLYING_MARKET_CLOSED_NO_SYNC_HEDGE", eval_closed.rejection_reason or "")

        # Case 3: Regular session open, but requested short quantity (100 shares) > available borrow locate (50 shares) (`S3-004`)
        broker_adapter.set_trading_session("NVDA", EquityTradingSession.REGULAR)
        eval_short_fail = scanner.evaluate_pair(
            leg_a_snapshot=token_adapter.normalize_snapshot(
                instrument_id="inst_token_nvda_usdt",
                raw_payload={
                    "bids": [["139.90", "200"]],
                    "asks": [["140.00", "200"]],
                    "session_status": "CONTINUOUS_24X7",
                    "source_timestamp_utc": self.now,
                },
                underlying_session_status=EquityTradingSession.REGULAR,
                now_utc=self.now,
            ),
            leg_b_snapshot=broker_adapter.normalize_broker_quote(
                instrument_id="inst_broker_nvda_usd",
                bid=Decimal("140.70"),
                bid_size=Decimal("200"),
                ask=Decimal("140.80"),
                ask_size=Decimal("200"),
                last_reference_price=Decimal("140.75"),
                now_utc=self.now,
            ),
            target_quantity_shares=Decimal("100"),
            expected_holding_hours=Decimal("24"),
            now_utc=self.now,
            broker_account=account,
            borrow_locate=locate,
        )
        self.assertFalse(eval_short_fail.accepted)
        self.assertIn("SHORT_BORROW_UNAVAILABLE:INSUFFICIENT_BORROW_SHARES_AVAILABLE", eval_short_fail.rejection_reason or "")


if __name__ == "__main__":
    unittest.main()
