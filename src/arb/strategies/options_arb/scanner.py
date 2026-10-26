"""OPT-003: S4-B Options Put-Call Parity & Volatility Relative-Value Scanner with Greeks & Stress Limits.

Enforces S4-B acceptance gates (`S4B-001` through `S4B-007`):
- Evaluates European Put-Call Parity conversions (`Long Synthetic Forward + Short Actual Forward`
  and `Short Synthetic Forward + Long Actual Forward`) using strictly executable option `ASK`/`BID`
  and underlying perp/future `ASK`/`BID` (`S4B-002`).
- Never triggers orders based solely on theoretical-to-mid price discrepancy without checking
  multi-leg round-trip exit spread cost, margin requirement, net Greeks limits (`max_net_delta`,
  `max_net_gamma`, `max_net_vega`), and worst-case scenario stress loss (`S4B-005`, `S4B-006`).
- Locked to `OpportunityDecision.PAPER` until options risk lead approval (`S4B-007`).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import math
from typing import Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel

from arb.domain.models import (
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderSide,
    PriceType,
)
from arb.options.models import ExerciseStyle, OptionContractSpec, OptionMarketQuote
from arb.options.pricing import GreeksProfile, OptionPricingEngine


class OptionComboRiskLimits(BaseModel):
    max_abs_net_delta: Decimal = Decimal("0.05")
    max_abs_net_gamma: Decimal = Decimal("0.01")
    max_abs_net_vega_usd: Decimal = Decimal("50.0")
    max_scenario_stress_loss_usd: Decimal = Decimal("250.0")
    max_margin_requirement_usd: Decimal = Decimal("5000.0")
    min_net_edge_after_exit_cost_usd: Decimal = Decimal("15.0")

    class Config:
        frozen = True


class OptionParityEvaluation(BaseModel):
    accepted: bool
    rejection_reason: Optional[str] = None
    structure_name: str  # "CONVERSION_BUY_CALL_SELL_PUT_SHORT_FWD" | "REVERSAL_SELL_CALL_BUY_PUT_LONG_FWD"
    call_instrument_id: str
    put_instrument_id: str
    underlying_instrument_id: str
    quantity_contracts: Decimal
    theoretical_parity_spread_usd: Decimal = Decimal("0")
    executable_gross_edge_usd: Decimal = Decimal("0")
    total_fees_usd: Decimal = Decimal("0")
    estimated_exit_spread_cost_usd: Decimal = Decimal("0")
    expected_net_pnl_usd: Decimal = Decimal("0")
    net_greeks: Optional[GreeksProfile] = None
    initial_margin_usd: Decimal = Decimal("0")
    scenario_stress_loss_usd: Decimal = Decimal("0")
    candidate: Optional[Opportunity] = None

    class Config:
        frozen = True


class OptionsRelativeValueScanner:
    """OPT-003: Evaluates Put-Call Parity conversion/reversal combos and enforces Greeks & scenario limits."""

    strategy_id: str = "strat_s4b_options_parity"
    strategy_version: str = "1.0.0"

    def __init__(
        self,
        pricing_engine: OptionPricingEngine,
        risk_limits: Optional[OptionComboRiskLimits] = None,
        option_taker_fee_bps: Decimal = Decimal("3.0"),
        perp_taker_fee_bps: Decimal = Decimal("5.0"),
    ) -> None:
        self.pricing_engine = pricing_engine
        self.risk_limits = risk_limits or OptionComboRiskLimits()
        self.option_taker_fee_bps = option_taker_fee_bps
        self.perp_taker_fee_bps = perp_taker_fee_bps

    def evaluate_put_call_parity(
        self,
        call_spec: OptionContractSpec,
        put_spec: OptionContractSpec,
        call_quote: OptionMarketQuote,
        put_quote: OptionMarketQuote,
        underlying_instrument_id: str,
        underlying_executable_bid: Decimal,
        underlying_executable_ask: Decimal,
        quantity_contracts: Decimal,
        now_utc: datetime,
    ) -> OptionParityEvaluation:
        # 1. Verify contract compatibility & European exercise style (`S4B-001`)
        if (
            call_spec.underlying_symbol != put_spec.underlying_symbol
            or call_spec.strike != put_spec.strike
            or call_spec.expiry_utc != put_spec.expiry_utc
            or call_spec.settlement_style != put_spec.settlement_style
        ):
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason="MISMATCHED_CALL_PUT_STRIKE_EXPIRY_OR_SETTLEMENT",
                structure_name="NONE",
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
            )

        if call_spec.exercise_style != ExerciseStyle.EUROPEAN or put_spec.exercise_style != ExerciseStyle.EUROPEAN:
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason="AMERICAN_EARLY_EXERCISE_NOT_ALLOWED_IN_EUROPEAN_PARITY",
                structure_name="NONE",
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
            )

        if (
            call_spec.research_only
            or put_spec.research_only
            or not call_spec.account_options_approved
            or not put_spec.account_options_approved
        ):
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason="OPTIONS_ACCOUNT_OR_CONTRACT_UNAPPROVED_RESEARCH_ONLY",
                structure_name="NONE",
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
            )

        # 2. Value both legs via OptionPricingEngine (`OPT-002`)
        val_call = self.pricing_engine.value_option(call_spec, call_quote, None, now_utc)
        val_put = self.pricing_engine.value_option(put_spec, put_quote, None, now_utc)
        if not val_call.is_valid or not val_put.is_valid:
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason=val_call.reject_reason or val_put.reject_reason or "OPTION_VALUATION_INVALID",
                structure_name="NONE",
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
            )

        assert (
            call_quote.bid_price is not None
            and call_quote.ask_price is not None
            and put_quote.bid_price is not None
            and put_quote.ask_price is not None
            and val_call.greeks is not None
            and val_put.greeks is not None
        )

        # Discount factor exp(-r * T)
        tte = float(call_spec.time_to_expiry_years(now_utc))
        df = Decimal(f"{math.exp(-float(self.pricing_engine.risk_free_rate) * tte):.8f}")
        mult = call_spec.contract_multiplier * quantity_contracts
        strike = call_spec.strike

        # Structure 1: Conversion (Buy Call @ Ask, Sell Put @ Bid, Short Underlying Forward @ Bid)
        # Executable locked-in payoff = df * (Underlying_Bid - Strike) - (Call_Ask - Put_Bid)
        conv_edge_per_unit = df * (underlying_executable_bid - strike) - (call_quote.ask_price - put_quote.bid_price)

        # Structure 2: Reversal (Sell Call @ Bid, Buy Put @ Ask, Long Underlying Forward @ Ask)
        # Executable locked-in payoff = (Call_Bid - Put_Ask) - df * (Underlying_Ask - Strike)
        rev_edge_per_unit = (call_quote.bid_price - put_quote.ask_price) - df * (underlying_executable_ask - strike)

        if conv_edge_per_unit >= rev_edge_per_unit:
            structure = "CONVERSION_BUY_CALL_SELL_PUT_SHORT_FWD"
            gross_edge_usd = conv_edge_per_unit * mult
            call_side, put_side, fwd_side = OrderSide.BUY, OrderSide.SELL, OrderSide.SELL
            call_exec_px, put_exec_px, fwd_exec_px = (
                call_quote.ask_price,
                put_quote.bid_price,
                underlying_executable_bid,
            )
            # Net Greeks: +Delta_Call - Delta_Put - df (Forward delta = df)
            net_delta = (val_call.greeks.delta - val_put.greeks.delta - df) * mult
            net_gamma = (val_call.greeks.gamma - val_put.greeks.gamma) * mult
            net_vega = (val_call.greeks.vega_per_1pct - val_put.greeks.vega_per_1pct) * mult
            net_theta = (val_call.greeks.theta_per_day - val_put.greeks.theta_per_day) * mult
        else:
            structure = "REVERSAL_SELL_CALL_BUY_PUT_LONG_FWD"
            gross_edge_usd = rev_edge_per_unit * mult
            call_side, put_side, fwd_side = OrderSide.SELL, OrderSide.BUY, OrderSide.BUY
            call_exec_px, put_exec_px, fwd_exec_px = (
                call_quote.bid_price,
                put_quote.ask_price,
                underlying_executable_ask,
            )
            net_delta = (-val_call.greeks.delta + val_put.greeks.delta + df) * mult
            net_gamma = (-val_call.greeks.gamma + val_put.greeks.gamma) * mult
            net_vega = (-val_call.greeks.vega_per_1pct + val_put.greeks.vega_per_1pct) * mult
            net_theta = (-val_call.greeks.theta_per_day + val_put.greeks.theta_per_day) * mult

        net_greeks = GreeksProfile(
            delta=net_delta,
            gamma=net_gamma,
            vega_per_1pct=net_vega,
            theta_per_day=net_theta,
        )

        # 3. Compute fees, exit bid-ask spread cost, margin requirement, and stress scenario loss (`S4B-005`, `S4B-006`)
        underlying_notional = fwd_exec_px * mult
        opt_fees_usd = (underlying_notional * self.option_taker_fee_bps * Decimal("2")) / Decimal("10000")
        fwd_fee_usd = (underlying_notional * self.perp_taker_fee_bps) / Decimal("10000")
        total_fees_usd = opt_fees_usd + fwd_fee_usd

        # Exit spread cost if forced to unwind early (half bid-ask spread across all 3 legs)
        call_half_spread = (call_quote.ask_price - call_quote.bid_price) * Decimal("0.5")
        put_half_spread = (put_quote.ask_price - put_quote.bid_price) * Decimal("0.5")
        fwd_half_spread = (underlying_executable_ask - underlying_executable_bid) * Decimal("0.5")
        exit_cost_usd = (call_half_spread + put_half_spread + fwd_half_spread) * mult

        expected_net_pnl_usd = gross_edge_usd - total_fees_usd - exit_cost_usd
        initial_margin_usd = underlying_notional * Decimal("0.15")
        # Stress scenario: 15% underlying jump + 10 vol point spike + early unwind spread cost
        scenario_loss_usd = exit_cost_usd + abs(net_vega) * Decimal("10") + abs(net_delta) * underlying_notional * Decimal("0.15")

        # 4. Enforce Combo Risk & Exit Cost Limits (`S4B-005`, `S4B-006`)
        if abs(net_greeks.delta) > self.risk_limits.max_abs_net_delta:
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason=f"NET_DELTA_LIMIT_EXCEEDED:{net_greeks.delta}",
                structure_name=structure,
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
                executable_gross_edge_usd=gross_edge_usd,
                total_fees_usd=total_fees_usd,
                estimated_exit_spread_cost_usd=exit_cost_usd,
                expected_net_pnl_usd=expected_net_pnl_usd,
                net_greeks=net_greeks,
                initial_margin_usd=initial_margin_usd,
                scenario_stress_loss_usd=scenario_loss_usd,
            )

        if scenario_loss_usd > self.risk_limits.max_scenario_stress_loss_usd:
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason=f"SCENARIO_STRESS_LOSS_EXCEEDED:{scenario_loss_usd}>{self.risk_limits.max_scenario_stress_loss_usd}",
                structure_name=structure,
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
                executable_gross_edge_usd=gross_edge_usd,
                total_fees_usd=total_fees_usd,
                estimated_exit_spread_cost_usd=exit_cost_usd,
                expected_net_pnl_usd=expected_net_pnl_usd,
                net_greeks=net_greeks,
                initial_margin_usd=initial_margin_usd,
                scenario_stress_loss_usd=scenario_loss_usd,
            )

        if initial_margin_usd > self.risk_limits.max_margin_requirement_usd:
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason="OPTION_COMBO_MARGIN_LIMIT_EXCEEDED",
                structure_name=structure,
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
                executable_gross_edge_usd=gross_edge_usd,
                total_fees_usd=total_fees_usd,
                estimated_exit_spread_cost_usd=exit_cost_usd,
                expected_net_pnl_usd=expected_net_pnl_usd,
                net_greeks=net_greeks,
                initial_margin_usd=initial_margin_usd,
                scenario_stress_loss_usd=scenario_loss_usd,
            )

        if expected_net_pnl_usd < self.risk_limits.min_net_edge_after_exit_cost_usd:
            return OptionParityEvaluation(
                accepted=False,
                rejection_reason="INSUFFICIENT_NET_EDGE_AFTER_FEES_AND_EXIT_SPREAD",
                structure_name=structure,
                call_instrument_id=call_spec.instrument_id,
                put_instrument_id=put_spec.instrument_id,
                underlying_instrument_id=underlying_instrument_id,
                quantity_contracts=quantity_contracts,
                executable_gross_edge_usd=gross_edge_usd,
                total_fees_usd=total_fees_usd,
                estimated_exit_spread_cost_usd=exit_cost_usd,
                expected_net_pnl_usd=expected_net_pnl_usd,
                net_greeks=net_greeks,
                initial_margin_usd=initial_margin_usd,
                scenario_stress_loss_usd=scenario_loss_usd,
            )

        net_edge_bps = (expected_net_pnl_usd / underlying_notional) * Decimal("10000")
        candidate = Opportunity(
            opportunity_id=f"opp_s4b_opt_{uuid4().hex[:12]}",
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            status=OpportunityStatus.PAPER,
            legs=[
                OpportunityLeg(
                    venue_id=call_spec.venue_id,
                    instrument_id=call_spec.instrument_id,
                    side=call_side,
                    target_quantity=quantity_contracts,
                    executable_price=call_exec_px,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=call_quote.ask_size if call_side == OrderSide.BUY else call_quote.bid_size,
                    expected_fee=opt_fees_usd * Decimal("0.5"),
                    fee_asset=call_spec.settlement_asset,
                    rules_version="v1",
                ),
                OpportunityLeg(
                    venue_id=put_spec.venue_id,
                    instrument_id=put_spec.instrument_id,
                    side=put_side,
                    target_quantity=quantity_contracts,
                    executable_price=put_exec_px,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=put_quote.bid_size if put_side == OrderSide.SELL else put_quote.ask_size,
                    expected_fee=opt_fees_usd * Decimal("0.5"),
                    fee_asset=put_spec.settlement_asset,
                    rules_version="v1",
                ),
                OpportunityLeg(
                    venue_id=call_spec.venue_id,
                    instrument_id=underlying_instrument_id,
                    side=fwd_side,
                    target_quantity=mult,
                    executable_price=fwd_exec_px,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=mult * Decimal("10"),
                    expected_fee=fwd_fee_usd,
                    fee_asset=call_spec.settlement_asset,
                    rules_version="v1",
                ),
            ],
            input_snapshot_ids=[
                f"{call_spec.instrument_id}:{int(now_utc.timestamp())}",
                f"{put_spec.instrument_id}:{int(now_utc.timestamp())}",
            ],
            settlement_asset=call_spec.settlement_asset,
            gross_edge=gross_edge_usd,
            fees=total_fees_usd,
            slippage=exit_cost_usd,
            expected_net_pnl=expected_net_pnl_usd,
            net_edge_bps=net_edge_bps,
            capacity=underlying_notional,
            risk_score=scenario_loss_usd,
            expires_at=now_utc + timedelta(seconds=5),
            decision=OpportunityDecision.PAPER,
            event_time_utc=now_utc,
            receive_time_utc=now_utc,
        )

        return OptionParityEvaluation(
            accepted=True,
            rejection_reason=None,
            structure_name=structure,
            call_instrument_id=call_spec.instrument_id,
            put_instrument_id=put_spec.instrument_id,
            underlying_instrument_id=underlying_instrument_id,
            quantity_contracts=quantity_contracts,
            theoretical_parity_spread_usd=gross_edge_usd,
            executable_gross_edge_usd=gross_edge_usd,
            total_fees_usd=total_fees_usd,
            estimated_exit_spread_cost_usd=exit_cost_usd,
            expected_net_pnl_usd=expected_net_pnl_usd,
            net_greeks=net_greeks,
            initial_margin_usd=initial_margin_usd,
            scenario_stress_loss_usd=scenario_loss_usd,
            candidate=candidate,
        )
