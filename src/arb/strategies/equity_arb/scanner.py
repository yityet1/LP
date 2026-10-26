"""EQ-004: S3 Equity-linked cross-market arbitrage scanner (Token/Perp vs Traditional Stock/Perp).

Enforces all S3 acceptance gates (`S3-001` through `S3-010`):
- `S3-001` & `S3-002`: Product terms and pair comparability must be explicitly approved;
  unapproved or `research_only` instruments/pairs are strictly rejected before generating executable candidates.
- `S3-003`: Trading session overlap and settlement gap gate — when synchronous hedging is required
  and either leg is `CLOSED` or `HALTED` (or in extended hours when extended hours execution is disabled),
  the candidate is rejected (`UNDERLYING_MARKET_CLOSED_NO_SYNC_HEDGE`).
- `S3-004`: Short-sale and borrow locate gate — selling a traditional stock leg without sufficient
  settled inventory requires `short_sale_allowed=True` and an active `BorrowLocateQuote` covering
  the short quantity.
- `S3-005`: Full equity cost breakdown — deducts crypto leg fee, broker commission/SEC fee,
  stock borrow cost over expected holding period, FX conversion cost (`USD <-> USDT`),
  redemption/issuance fee (if conversion path), and product terms holding basis risk haircut.
- `S3-006`: Corporate action freeze gate — immediately halts/rejects any candidate on active or
  upcoming dividend, stock split, merger, or symbol change freeze windows.
- `S3-007`: Executable orderbook price discipline — strictly evaluates BUY at executable `ASK`
  (VWAP) and SELL at executable `BID` (VWAP), never using `mark_price` or `index_reference_price`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.domain.models import (
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderSide,
    PriceType,
)
from arb.equities.adapters import (
    BorrowLocateQuote,
    EquityBrokerAccountSnapshot,
    EquityMarketSnapshot,
)
from arb.equities.terms import (
    EquityProductKind,
    EquityTermsRegistry,
    EquityTradingSession,
)


class EquityArbEvaluation(BaseModel):
    """Detailed evaluation record for an S3 equity-linked cross-market opportunity."""

    buy_instrument_id: str
    sell_instrument_id: str
    underlying_ticker: str
    accepted: bool
    rejection_reason: Optional[str] = None
    buy_vwap_price: Optional[Decimal] = None
    sell_vwap_price: Optional[Decimal] = None
    quantity_shares: Decimal = Decimal("0")
    gross_edge_bps: Decimal = Decimal("0")
    total_cost_bps: Decimal = Decimal("0")
    borrow_cost_bps: Decimal = Decimal("0")
    fx_conversion_cost_bps: Decimal = Decimal("0")
    basis_risk_haircut_bps: Decimal = Decimal("0")
    net_edge_bps: Decimal = Decimal("0")
    expected_net_pnl_usd: Decimal = Decimal("0")
    session_overlap_valid: bool = False
    product_terms_versions: Tuple[str, str] = ("", "")
    residual_risks: Tuple[str, ...] = Field(default_factory=tuple)
    candidate: Optional[Opportunity] = None

    class Config:
        frozen = True


class EquityCrossMarketScanner:
    """EQ-004: Scans approved equity-linked pairs across tokenized equities, equity perps, and brokers."""

    strategy_id: str = "strat_s3_equity_cross_market"
    strategy_version: str = "1.0.0"

    def __init__(
        self,
        terms_registry: EquityTermsRegistry,
        min_net_edge_bps: Decimal = Decimal("8.0"),
        max_cross_venue_skew_ms: int = 500,
        crypto_taker_fee_bps: Decimal = Decimal("5.0"),
        broker_commission_bps: Decimal = Decimal("2.0"),
        fx_usd_usdt_cost_bps: Decimal = Decimal("3.0"),
    ) -> None:
        self.terms_registry = terms_registry
        self.min_net_edge_bps = min_net_edge_bps
        self.max_cross_venue_skew_ms = max_cross_venue_skew_ms
        self.crypto_taker_fee_bps = crypto_taker_fee_bps
        self.broker_commission_bps = broker_commission_bps
        self.fx_usd_usdt_cost_bps = fx_usd_usdt_cost_bps

    def evaluate_pair(
        self,
        leg_a_snapshot: EquityMarketSnapshot,
        leg_b_snapshot: EquityMarketSnapshot,
        target_quantity_shares: Decimal,
        expected_holding_hours: Decimal,
        now_utc: datetime,
        broker_account: Optional[EquityBrokerAccountSnapshot] = None,
        borrow_locate: Optional[BorrowLocateQuote] = None,
        allow_extended_hours: bool = False,
    ) -> EquityArbEvaluation:
        """Evaluate buying the cheaper equity leg and selling the richer equity leg."""
        # Step 1: Validate product terms, comparability approval, and corporate action freeze (S3-001, S3-002, S3-006)
        valid_pair, pair_reason, approval, terms_a, terms_b = self.terms_registry.validate_pair_for_execution(
            leg_a_instrument_id=leg_a_snapshot.instrument_id,
            leg_b_instrument_id=leg_b_snapshot.instrument_id,
            now_utc=now_utc,
        )
        ticker = terms_a.underlying_ticker if terms_a else (terms_b.underlying_ticker if terms_b else "UNKNOWN")
        versions = (
            leg_a_snapshot.product_terms_version,
            leg_b_snapshot.product_terms_version,
        )
        if not valid_pair or approval is None or terms_a is None or terms_b is None:
            return EquityArbEvaluation(
                buy_instrument_id=leg_a_snapshot.instrument_id,
                sell_instrument_id=leg_b_snapshot.instrument_id,
                underlying_ticker=ticker,
                accepted=False,
                rejection_reason=pair_reason or "UNAPPROVED_PRODUCT_TERMS",
                product_terms_versions=versions,
            )

        # Step 2: Check Trading Session Overlap & Synchronous Hedge Executability (S3-003)
        for snap, terms in ((leg_a_snapshot, terms_a), (leg_b_snapshot, terms_b)):
            if snap.session_status in (EquityTradingSession.CLOSED, EquityTradingSession.HALTED):
                return EquityArbEvaluation(
                    buy_instrument_id=leg_a_snapshot.instrument_id,
                    sell_instrument_id=leg_b_snapshot.instrument_id,
                    underlying_ticker=ticker,
                    accepted=False,
                    rejection_reason=f"UNDERLYING_MARKET_CLOSED_NO_SYNC_HEDGE:{snap.instrument_id}:{snap.session_status.value}",
                    product_terms_versions=versions,
                )
            if snap.session_status in (EquityTradingSession.PRE_MARKET, EquityTradingSession.AFTER_HOURS) and not allow_extended_hours:
                return EquityArbEvaluation(
                    buy_instrument_id=leg_a_snapshot.instrument_id,
                    sell_instrument_id=leg_b_snapshot.instrument_id,
                    underlying_ticker=ticker,
                    accepted=False,
                    rejection_reason=f"EXTENDED_HOURS_DISABLED:{snap.instrument_id}:{snap.session_status.value}",
                    product_terms_versions=versions,
                )
            if approval.requires_synchronous_session and not snap.underlying_hedge_executable:
                return EquityArbEvaluation(
                    buy_instrument_id=leg_a_snapshot.instrument_id,
                    sell_instrument_id=leg_b_snapshot.instrument_id,
                    underlying_ticker=ticker,
                    accepted=False,
                    rejection_reason=f"UNDERLYING_MARKET_CLOSED_NO_SYNC_HEDGE:{snap.instrument_id}",
                    product_terms_versions=versions,
                )

        # Step 3: Verify Executable Orderbook Quotes Exist (S3-007: never fallback to mark/reference price)
        if not leg_a_snapshot.is_quote_executable or not leg_b_snapshot.is_quote_executable:
            return EquityArbEvaluation(
                buy_instrument_id=leg_a_snapshot.instrument_id,
                sell_instrument_id=leg_b_snapshot.instrument_id,
                underlying_ticker=ticker,
                accepted=False,
                rejection_reason="MISSING_EXECUTABLE_ORDERBOOK_QUOTES",
                product_terms_versions=versions,
            )

        # Check cross-venue timestamp skew
        skew_ms = abs(
            int((leg_a_snapshot.source_timestamp_utc - leg_b_snapshot.source_timestamp_utc).total_seconds() * 1000)
        )
        if skew_ms > self.max_cross_venue_skew_ms:
            return EquityArbEvaluation(
                buy_instrument_id=leg_a_snapshot.instrument_id,
                sell_instrument_id=leg_b_snapshot.instrument_id,
                underlying_ticker=ticker,
                accepted=False,
                rejection_reason=f"CROSS_VENUE_CLOCK_SKEW_EXCEEDED:{skew_ms}ms",
                product_terms_versions=versions,
            )

        # Step 4: Determine direction (buy cheaper ASK, sell higher BID) using VWAP across L2 depth
        ok_buy_a, vwap_buy_a, avail_buy_a = leg_a_snapshot.estimate_vwap(OrderSide.BUY, target_quantity_shares)
        ok_sell_b, vwap_sell_b, avail_sell_b = leg_b_snapshot.estimate_vwap(OrderSide.SELL, target_quantity_shares)
        ok_buy_b, vwap_buy_b, avail_buy_b = leg_b_snapshot.estimate_vwap(OrderSide.BUY, target_quantity_shares)
        ok_sell_a, vwap_sell_a, avail_sell_a = leg_a_snapshot.estimate_vwap(OrderSide.SELL, target_quantity_shares)

        edge_a_to_b = Decimal("-999999")
        if ok_buy_a and ok_sell_b and vwap_buy_a and vwap_sell_b and vwap_buy_a > Decimal("0"):
            edge_a_to_b = ((vwap_sell_b - vwap_buy_a) / vwap_buy_a) * Decimal("10000")

        edge_b_to_a = Decimal("-999999")
        if ok_buy_b and ok_sell_a and vwap_buy_b and vwap_sell_a and vwap_buy_b > Decimal("0"):
            edge_b_to_a = ((vwap_sell_a - vwap_buy_b) / vwap_buy_b) * Decimal("10000")

        if edge_a_to_b == Decimal("-999999") and edge_b_to_a == Decimal("-999999"):
            return EquityArbEvaluation(
                buy_instrument_id=leg_a_snapshot.instrument_id,
                sell_instrument_id=leg_b_snapshot.instrument_id,
                underlying_ticker=ticker,
                accepted=False,
                rejection_reason="INSUFFICIENT_EXECUTABLE_DEPTH",
                product_terms_versions=versions,
            )

        if edge_a_to_b >= edge_b_to_a:
            buy_snap, sell_snap = leg_a_snapshot, leg_b_snapshot
            buy_terms, sell_terms = terms_a, terms_b
            buy_vwap, sell_vwap = vwap_buy_a, vwap_sell_b
            avail_buy, avail_sell = avail_buy_a, avail_sell_b
            gross_edge_bps = edge_a_to_b
        else:
            buy_snap, sell_snap = leg_b_snapshot, leg_a_snapshot
            buy_terms, sell_terms = terms_b, terms_a
            buy_vwap, sell_vwap = vwap_buy_b, vwap_sell_a
            avail_buy, avail_sell = avail_buy_b, avail_sell_a
            gross_edge_bps = edge_b_to_a

        assert buy_vwap is not None and sell_vwap is not None

        # Step 5: Holding period & Short-sale / Borrow Locate Check on SELL leg (S3-004)
        if expected_holding_hours > approval.max_holding_hours:
            return EquityArbEvaluation(
                buy_instrument_id=buy_snap.instrument_id,
                sell_instrument_id=sell_snap.instrument_id,
                underlying_ticker=ticker,
                accepted=False,
                rejection_reason="EXPECTED_HOLDING_EXCEEDS_MAX_APPROVED_HOURS",
                buy_vwap_price=buy_vwap,
                sell_vwap_price=sell_vwap,
                quantity_shares=target_quantity_shares,
                gross_edge_bps=gross_edge_bps,
                product_terms_versions=(buy_snap.product_terms_version, sell_snap.product_terms_version),
            )

        borrow_cost_bps = Decimal("0")
        if sell_terms.product_kind == EquityProductKind.TRADITIONAL_EQUITY:
            settled_shares = (
                broker_account.positions_shares.get(ticker, Decimal("0"))
                if broker_account is not None
                else Decimal("0")
            )
            short_shares_needed = max(Decimal("0"), target_quantity_shares - settled_shares)
            if short_shares_needed > Decimal("0"):
                if not sell_terms.short_sale_allowed:
                    return EquityArbEvaluation(
                        buy_instrument_id=buy_snap.instrument_id,
                        sell_instrument_id=sell_snap.instrument_id,
                        underlying_ticker=ticker,
                        accepted=False,
                        rejection_reason="SHORT_BORROW_UNAVAILABLE:SHORT_SALE_FORBIDDEN",
                        buy_vwap_price=buy_vwap,
                        sell_vwap_price=sell_vwap,
                        quantity_shares=target_quantity_shares,
                        gross_edge_bps=gross_edge_bps,
                        product_terms_versions=(buy_snap.product_terms_version, sell_snap.product_terms_version),
                    )
                if borrow_locate is None:
                    return EquityArbEvaluation(
                        buy_instrument_id=buy_snap.instrument_id,
                        sell_instrument_id=sell_snap.instrument_id,
                        underlying_ticker=ticker,
                        accepted=False,
                        rejection_reason="SHORT_BORROW_UNAVAILABLE:MISSING_LOCATE_QUOTE",
                        buy_vwap_price=buy_vwap,
                        sell_vwap_price=sell_vwap,
                        quantity_shares=target_quantity_shares,
                        gross_edge_bps=gross_edge_bps,
                        product_terms_versions=(buy_snap.product_terms_version, sell_snap.product_terms_version),
                    )
                can_short, short_reason = borrow_locate.can_short(short_shares_needed, now_utc)
                if not can_short:
                    return EquityArbEvaluation(
                        buy_instrument_id=buy_snap.instrument_id,
                        sell_instrument_id=sell_snap.instrument_id,
                        underlying_ticker=ticker,
                        accepted=False,
                        rejection_reason=f"SHORT_BORROW_UNAVAILABLE:{short_reason}",
                        buy_vwap_price=buy_vwap,
                        sell_vwap_price=sell_vwap,
                        quantity_shares=target_quantity_shares,
                        gross_edge_bps=gross_edge_bps,
                        product_terms_versions=(buy_snap.product_terms_version, sell_snap.product_terms_version),
                    )
                # Pro-rate annualized borrow rate over expected holding hours
                borrow_cost_bps = (
                    borrow_locate.annualized_borrow_rate_bps
                    * (expected_holding_hours / Decimal("8760"))
                )

        # Step 6: Full Cost & Basis Risk Haircut Breakdown (S3-005)
        trading_fees_bps = self.crypto_taker_fee_bps + self.broker_commission_bps
        fx_cost_bps = self.fx_usd_usdt_cost_bps
        redemption_bps = (
            max(buy_terms.redemption_fee_bps, sell_terms.redemption_fee_bps)
            if approval.allow_cross_market_conversion
            else Decimal("0")
        )
        basis_haircut_bps = max(
            approval.min_convergence_haircut_bps,
            buy_terms.holding_basis_risk_haircut_bps,
            sell_terms.holding_basis_risk_haircut_bps,
        )
        total_cost_bps = trading_fees_bps + fx_cost_bps + borrow_cost_bps + redemption_bps + basis_haircut_bps
        net_edge_bps = gross_edge_bps - total_cost_bps
        notional_usd = buy_vwap * target_quantity_shares
        gross_edge_usd = (sell_vwap - buy_vwap) * target_quantity_shares
        fees_usd = (notional_usd * (trading_fees_bps + fx_cost_bps + redemption_bps)) / Decimal("10000")
        carry_usd = (notional_usd * (borrow_cost_bps + basis_haircut_bps)) / Decimal("10000")
        expected_net_pnl_usd = (notional_usd * net_edge_bps) / Decimal("10000")

        residual_risks = (
            f"settlement_gap:{buy_terms.settlement_cycle.value}_vs_{sell_terms.settlement_cycle.value}",
            f"redemption_mode:{buy_terms.redemption_rights.value}/{sell_terms.redemption_rights.value}",
            f"dividend_treatment:{buy_terms.dividend_treatment.value}/{sell_terms.dividend_treatment.value}",
        )

        if net_edge_bps < self.min_net_edge_bps:
            return EquityArbEvaluation(
                buy_instrument_id=buy_snap.instrument_id,
                sell_instrument_id=sell_snap.instrument_id,
                underlying_ticker=ticker,
                accepted=False,
                rejection_reason="INSUFFICIENT_NET_EDGE_AFTER_BORROW_FX_AND_BASIS_HAIRCUT",
                buy_vwap_price=buy_vwap,
                sell_vwap_price=sell_vwap,
                quantity_shares=target_quantity_shares,
                gross_edge_bps=gross_edge_bps,
                total_cost_bps=total_cost_bps,
                borrow_cost_bps=borrow_cost_bps,
                fx_conversion_cost_bps=fx_cost_bps,
                basis_risk_haircut_bps=basis_haircut_bps,
                net_edge_bps=net_edge_bps,
                expected_net_pnl_usd=expected_net_pnl_usd,
                session_overlap_valid=True,
                product_terms_versions=(buy_snap.product_terms_version, sell_snap.product_terms_version),
                residual_risks=residual_risks,
            )

        buy_fee_bps = (
            self.crypto_taker_fee_bps
            if buy_terms.product_kind != EquityProductKind.TRADITIONAL_EQUITY
            else self.broker_commission_bps
        )
        sell_fee_bps = (
            self.broker_commission_bps
            if sell_terms.product_kind == EquityProductKind.TRADITIONAL_EQUITY
            else self.crypto_taker_fee_bps
        )

        candidate = Opportunity(
            opportunity_id=f"opp_s3_eq_{uuid4().hex[:12]}",
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            status=OpportunityStatus.COSTED,
            legs=[
                OpportunityLeg(
                    venue_id=buy_snap.venue_id,
                    instrument_id=buy_snap.instrument_id,
                    side=OrderSide.BUY,
                    target_quantity=target_quantity_shares,
                    executable_price=buy_vwap,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=avail_buy,
                    expected_fee=(notional_usd * buy_fee_bps) / Decimal("10000"),
                    fee_asset="USD",
                    rules_version=buy_snap.product_terms_version,
                ),
                OpportunityLeg(
                    venue_id=sell_snap.venue_id,
                    instrument_id=sell_snap.instrument_id,
                    side=OrderSide.SELL,
                    target_quantity=target_quantity_shares,
                    executable_price=sell_vwap,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=avail_sell,
                    expected_fee=(notional_usd * sell_fee_bps) / Decimal("10000"),
                    fee_asset="USD",
                    rules_version=sell_snap.product_terms_version,
                ),
            ],
            input_snapshot_ids=[
                f"snap_{buy_snap.instrument_id}_{int(now_utc.timestamp())}",
                f"snap_{sell_snap.instrument_id}_{int(now_utc.timestamp())}",
            ],
            settlement_asset="USD",
            gross_edge=gross_edge_usd,
            fees=fees_usd,
            slippage=Decimal("0"),
            funding_or_carry=carry_usd,
            gas_or_bridge_cost=Decimal("0"),
            expected_net_pnl=expected_net_pnl_usd,
            net_edge_bps=net_edge_bps,
            capacity=notional_usd,
            risk_score=basis_haircut_bps,
            expires_at=now_utc + timedelta(seconds=5),
            decision=OpportunityDecision.PAPER,
            event_time_utc=now_utc,
            receive_time_utc=now_utc,
        )

        return EquityArbEvaluation(
            buy_instrument_id=buy_snap.instrument_id,
            sell_instrument_id=sell_snap.instrument_id,
            underlying_ticker=ticker,
            accepted=True,
            buy_vwap_price=buy_vwap,
            sell_vwap_price=sell_vwap,
            quantity_shares=target_quantity_shares,
            gross_edge_bps=gross_edge_bps,
            total_cost_bps=total_cost_bps,
            borrow_cost_bps=borrow_cost_bps,
            fx_conversion_cost_bps=fx_cost_bps,
            basis_risk_haircut_bps=basis_haircut_bps,
            net_edge_bps=net_edge_bps,
            expected_net_pnl_usd=expected_net_pnl_usd,
            session_overlap_valid=True,
            product_terms_versions=(buy_snap.product_terms_version, sell_snap.product_terms_version),
            residual_risks=residual_risks,
            candidate=candidate,
        )
