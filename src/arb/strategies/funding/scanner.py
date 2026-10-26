"""[DERIV-003] S2-B: Perpetual Funding Rate Arbitrage Engine & Margin/Liquidation Buffer Monitor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.config.settings import RiskPolicyConfig
from arb.costs.engine import CostEngine
from arb.domain.models import (
    CycleState,
    ExecutionPlan,
    ExecutionPlanLeg,
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderSide,
    OrderType,
    PositionSnapshot,
    PriceType,
    ensure_utc,
)
from arb.instruments.registry import InstrumentRegistry
from arb.marketdata.health import MarketHealthGate
from arb.marketdata.orderbook import OrderBookBuilder
from arb.strategy.lifecycle import (
    OpportunityLifecycleManager,
    OpportunityRejectReason,
)
from arb.venues.derivatives.adapters import DerivativeFundingSnapshot


class MarginSafetyAction(str, Enum):
    HEALTHY = "healthy"
    WARN_BLOCK_NEW_OPEN = "warn_block_new_open"
    CRITICAL_AUTO_DELEVERAGE = "critical_auto_deleverage"
    FUNDING_REVERSAL_UNWIND = "funding_reversal_unwind"


class MarginHealthReport(BaseModel):
    venue_id: str
    instrument_id: str
    mark_price: Decimal
    liquidation_price: Optional[Decimal]
    distance_to_liquidation_bps: Decimal
    margin_ratio: Decimal
    action: MarginSafetyAction
    allow_new_open: bool
    requires_emergency_unwind: bool
    reason_codes: List[str] = Field(default_factory=list)
    emergency_unwind_plan: Optional[ExecutionPlan] = None


class MarginAndLiquidationMonitor:
    """
    [DERIV-003 / S2-009 / S2-010 / S2-011] Monitors derivative margin ratio, mark-to-liquidation buffer,
    and funding rate sign reversals:
    - Uses `mark_price` strictly for liquidation buffer calculation (`distance_to_liquidation_bps`),
      while generating emergency unwind orders against executable L2 prices (`S2-001`, `S2-009`).
    - When `distance_to_liquidation_bps < warn_liquidation_buffer_bps` (default 1500 bps / 15%)
      or `margin_ratio > warn_margin_ratio` (default 0.65), blocks new opens (`WARN_BLOCK_NEW_OPEN`).
    - When `distance_to_liquidation_bps < critical_liquidation_buffer_bps` (default 800 bps / 8%)
      or `margin_ratio > critical_margin_ratio` (default 0.80), triggers `CRITICAL_AUTO_DELEVERAGE`
      and generates a paired 2-leg `ExecutionPlan` (closing both the derivative leg and the hedged spot/perp leg)
      so auto de-leveraging never leaves naked single-leg directional exposure (`S2-010`)!
    """

    def __init__(
        self,
        warn_liquidation_buffer_bps: Decimal = Decimal("1500.0"),      # 15% mark-to-liq buffer
        critical_liquidation_buffer_bps: Decimal = Decimal("800.0"),   # 8% mark-to-liq buffer -> auto deleverage
        warn_margin_ratio: Decimal = Decimal("0.65"),
        critical_margin_ratio: Decimal = Decimal("0.80"),
        deleverage_fraction: Decimal = Decimal("0.50"),
    ) -> None:
        self._warn_liq_bps = warn_liquidation_buffer_bps
        self._crit_liq_bps = critical_liquidation_buffer_bps
        self._warn_mgn = warn_margin_ratio
        self._crit_mgn = critical_margin_ratio
        self._deleverage_fraction = deleverage_fraction

    def evaluate_position_health(
        self,
        deriv_position: PositionSnapshot,
        account_equity_usdt: Decimal,
        paired_spot_venue_id: str,
        paired_spot_instrument_id: str,
        spot_executable_bid: Decimal,
        deriv_executable_ask: Decimal,
        config_version_hash: str,
        current_funding_rate: Optional[Decimal] = None,
        funding_reversal_threshold: Decimal = Decimal("-0.0003"),  # -3 bps flip triggers exit
    ) -> MarginHealthReport:
        mark_px = deriv_position.mark_price
        liq_px = deriv_position.liquidation_price

        if liq_px is not None and liq_px > Decimal("0") and mark_px > Decimal("0"):
            # For a short position (quantity < 0), liquidation price is above mark_price;
            # if mark_price >= liq_px, buffer is 0.
            if deriv_position.quantity < Decimal("0"):
                dist_bps = (
                    ((liq_px - mark_px) / mark_px) * Decimal("10000")
                    if liq_px > mark_px
                    else Decimal("0")
                )
            else:
                dist_bps = (
                    ((mark_px - liq_px) / mark_px) * Decimal("10000")
                    if mark_px > liq_px
                    else Decimal("0")
                )
        else:
            dist_bps = Decimal("99999")

        margin_ratio = (
            deriv_position.margin_used / account_equity_usdt
            if account_equity_usdt > Decimal("0")
            else Decimal("1.0")
        )

        reasons: List[str] = []
        action = MarginSafetyAction.HEALTHY
        allow_new = True
        requires_unwind = False

        if dist_bps < self._crit_liq_bps or margin_ratio >= self._crit_mgn:
            action = MarginSafetyAction.CRITICAL_AUTO_DELEVERAGE
            allow_new = False
            requires_unwind = True
            reasons.append(
                f"CRITICAL_LIQUIDATION_OR_MARGIN_BREACH:dist_bps={dist_bps:.1f},margin_ratio={margin_ratio:.2%}"
            )
        elif current_funding_rate is not None and current_funding_rate <= funding_reversal_threshold:
            action = MarginSafetyAction.FUNDING_REVERSAL_UNWIND
            allow_new = False
            requires_unwind = True
            reasons.append(f"FUNDING_RATE_REVERSAL_EXIT:rate={current_funding_rate}")
        elif dist_bps < self._warn_liq_bps or margin_ratio >= self._warn_mgn:
            action = MarginSafetyAction.WARN_BLOCK_NEW_OPEN
            allow_new = False
            reasons.append(
                f"WARN_LIQUIDATION_OR_MARGIN_BUFFER:dist_bps={dist_bps:.1f},margin_ratio={margin_ratio:.2%}"
            )

        unwind_plan: Optional[ExecutionPlan] = None
        if requires_unwind:
            abs_contracts = abs(deriv_position.quantity)
            reduce_contracts = (
                abs_contracts
                if action == MarginSafetyAction.FUNDING_REVERSAL_UNWIND
                else max(Decimal("1"), (abs_contracts * self._deleverage_fraction).to_integral_value(rounding=ROUND_DOWN))
            )
            reduce_spot_base = reduce_contracts * deriv_position.contract_multiplier
            cyc_id = f"cyc_delev_{uuid4().hex[:8]}"

            # Paired 2-leg reduce-only plan: BUY back short derivative leg AND SELL hedged spot leg simultaneously (`S2-010`)
            unwind_plan = ExecutionPlan(
                cycle_id=cyc_id,
                opportunity_id=f"opp_delev_{cyc_id}",
                strategy_id="strat_s2b_funding_rate_carry",
                strategy_version="1.0.0",
                execution_mode="paper",
                state=CycleState.PREPARED,
                legs=[
                    ExecutionPlanLeg(
                        client_order_id=f"{cyc_id}_close_deriv",
                        venue_id=deriv_position.venue_id,
                        instrument_id=deriv_position.instrument_id,
                        side=OrderSide.BUY if deriv_position.quantity < Decimal("0") else OrderSide.SELL,
                        order_type=OrderType.IOC,
                        quantity=reduce_contracts,
                        limit_price=deriv_executable_ask,
                        max_slippage_bps=Decimal("20"),
                        rules_version="v1",
                    ),
                    ExecutionPlanLeg(
                        client_order_id=f"{cyc_id}_close_spot",
                        venue_id=paired_spot_venue_id,
                        instrument_id=paired_spot_instrument_id,
                        side=OrderSide.SELL if deriv_position.quantity < Decimal("0") else OrderSide.BUY,
                        order_type=OrderType.IOC,
                        quantity=reduce_spot_base,
                        limit_price=spot_executable_bid,
                        max_slippage_bps=Decimal("20"),
                        rules_version="v1",
                    ),
                ],
                reserved_asset="USDT",
                reserved_amount=reduce_spot_base * spot_executable_bid,
                max_unhedged_notional=Decimal("500"),
                config_version_hash=config_version_hash,
            )

        return MarginHealthReport(
            venue_id=deriv_position.venue_id,
            instrument_id=deriv_position.instrument_id,
            mark_price=mark_px,
            liquidation_price=liq_px,
            distance_to_liquidation_bps=dist_bps,
            margin_ratio=margin_ratio,
            action=action,
            allow_new_open=allow_new,
            requires_emergency_unwind=requires_unwind,
            reason_codes=reasons,
            emergency_unwind_plan=unwind_plan,
        )


class FundingCarryPairSpec(BaseModel):
    pair_id: str
    long_venue_id: str
    long_instrument_id: str
    short_deriv_venue_id: str
    short_deriv_instrument_id: str
    target_base_qty: Decimal
    expected_holding_settlements: int = 3  # e.g., 3 x 8h = 24h holding horizon


class FundingCarryScanReport(BaseModel):
    opportunity: Opportunity
    standardized_8h_net_funding_bps: Decimal
    expected_total_funding_usdt: Decimal
    round_trip_fees_and_slippage_usdt: Decimal
    break_even_settlements: Decimal
    expected_net_carry_usdt: Decimal
    net_carry_bps: Decimal


class FundingRateArbitrageScanner:
    """
    [DERIV-003] S2-B Perpetual Funding Rate Carry & Cross-Venue Funding Arbitrage Engine.
    Enforces S2 acceptance gates:
    - `S2-001`: Uses L2 `ASK` on long leg and L2 `BID` on short perp leg for execution simulation;
      never uses `mark_price` or `index_price` as fill price.
    - `S2-003`: Aligns Spot/Perp `step_size` and `contract_multiplier` (`ctVal`) so delta mismatch is 0.
    - `S2-006` & `S2-007`: Normalizes funding intervals (`1h`, `4h`, `8h`) into standardized 8h bps,
      and rejects cross-venue perp-perp funding trades if `next_funding_time_utc` differs across venues
      beyond `max_settlement_clock_skew_seconds` (`FUNDING_SETTLEMENT_TIME_MISMATCH`).
    - `S2-008`: Deducts 4-leg round-trip taker fees and entry/exit slippage from expected funding income
      over `expected_holding_settlements`, rejecting if `expected_net_carry_usdt <= 0` or `net_carry_bps < min_net_edge_bps`.
    - `S2-009`: Integrates with `MarginHealthReport` to block new openings when margin/liquidation buffer is in warning/critical state.
    """

    STRATEGY_ID = "strat_s2b_funding_rate_carry"
    STRATEGY_VERSION = "1.0.0"

    def __init__(
        self,
        registry: InstrumentRegistry,
        health_gate: MarketHealthGate,
        cost_engine: CostEngine,
        lifecycle: OpportunityLifecycleManager,
        risk_policy: RiskPolicyConfig,
        max_settlement_clock_skew_seconds: int = 60,
        candidate_ttl_ms: int = 500,
    ) -> None:
        self._registry = registry
        self._health_gate = health_gate
        self._cost_engine = cost_engine
        self._lifecycle = lifecycle
        self._risk_policy = risk_policy
        self._max_settlement_skew_s = max_settlement_clock_skew_seconds
        self._candidate_ttl_ms = candidate_ttl_ms

    def evaluate_funding_carry(
        self,
        spec: FundingCarryPairSpec,
        order_books: Dict[Tuple[str, str], OrderBookBuilder],
        short_funding: DerivativeFundingSnapshot,
        long_funding: Optional[DerivativeFundingSnapshot] = None,
        margin_health: Optional[MarginHealthReport] = None,
        now_utc: Optional[datetime] = None,
    ) -> FundingCarryScanReport:
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        expires_at = ref_now + timedelta(milliseconds=self._candidate_ttl_ms)

        long_builder = order_books.get((spec.long_venue_id, spec.long_instrument_id))
        short_builder = order_books.get((spec.short_deriv_venue_id, spec.short_deriv_instrument_id))
        snap_ids: List[str] = []
        if long_builder:
            snap_ids.append(f"{spec.long_venue_id}:{spec.long_instrument_id}:{long_builder.last_update_id}")
        if short_builder:
            snap_ids.append(f"{spec.short_deriv_venue_id}:{spec.short_deriv_instrument_id}:{short_builder.last_update_id}")

        reject_reasons: List[str] = []
        if margin_health is not None and not margin_health.allow_new_open:
            reject_reasons.append("MARGIN_OR_LIQUIDATION_BUFFER_BREACH")
            reject_reasons.extend(margin_health.reason_codes)

        # Check funding settlement clock alignment for cross-venue perp-perp (`S2-007`)
        if long_funding is not None:
            skew_s = abs((short_funding.next_funding_time_utc - long_funding.next_funding_time_utc).total_seconds())
            if skew_s > self._max_settlement_skew_s:
                reject_reasons.append("FUNDING_SETTLEMENT_TIME_MISMATCH")

        try:
            long_rules = self._registry.get_rules(spec.long_venue_id, spec.long_instrument_id, now_utc=ref_now)
            short_rules = self._registry.get_rules(spec.short_deriv_venue_id, spec.short_deriv_instrument_id, now_utc=ref_now)
        except Exception:
            reject_reasons.append(OpportunityRejectReason.INSTRUMENT_NOT_TRADABLE.value)
            long_rules = None  # type: ignore[assignment]
            short_rules = None  # type: ignore[assignment]

        if long_builder is None or short_builder is None:
            reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
        else:
            health_ok, _, h_reasons = self._health_gate.evaluate_multi_book_gate(
                [long_builder, short_builder], now_utc=ref_now
            )
            if not health_ok:
                reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
                reject_reasons.extend(h_reasons)

        if reject_reasons or long_rules is None or short_rules is None or long_builder is None or short_builder is None:
            opp = self._rejected(snap_ids, expires_at, ref_now, reject_reasons)
            return FundingCarryScanReport(
                opportunity=opp,
                standardized_8h_net_funding_bps=Decimal("0"),
                expected_total_funding_usdt=Decimal("0"),
                round_trip_fees_and_slippage_usdt=Decimal("0"),
                break_even_settlements=Decimal("999"),
                expected_net_carry_usdt=Decimal("0"),
                net_carry_bps=Decimal("0"),
            )

        # Align effective base quantity across long leg and short derivative leg (`S2-003`)
        long_eff_step = long_rules.step_size * long_rules.contract_multiplier
        short_eff_step = short_rules.step_size * short_rules.contract_multiplier
        unit_base = max(long_eff_step, short_eff_step)
        units = (spec.target_base_qty / unit_base).to_integral_value(rounding=ROUND_DOWN)
        aligned_base = units * unit_base

        if aligned_base <= Decimal("0"):
            opp = self._rejected(snap_ids, expires_at, ref_now, [OpportunityRejectReason.STEP_OR_MIN_QTY_REJECT.value])
            return FundingCarryScanReport(
                opportunity=opp,
                standardized_8h_net_funding_bps=Decimal("0"),
                expected_total_funding_usdt=Decimal("0"),
                round_trip_fees_and_slippage_usdt=Decimal("0"),
                break_even_settlements=Decimal("999"),
                expected_net_carry_usdt=Decimal("0"),
                net_carry_bps=Decimal("0"),
            )

        long_qty = aligned_base / long_rules.contract_multiplier
        short_qty = aligned_base / short_rules.contract_multiplier

        long_sim = self._cost_engine.simulate_fill(
            long_builder.to_order_book(), long_rules, OrderSide.BUY, long_qty
        )
        short_sim = self._cost_engine.simulate_fill(
            short_builder.to_order_book(), short_rules, OrderSide.SELL, short_qty
        )

        if not long_sim.is_fully_executable or not short_sim.is_fully_executable:
            opp = self._rejected(
                snap_ids, expires_at, ref_now, [OpportunityRejectReason.INSUFFICIENT_DEPTH.value]
            )
            return FundingCarryScanReport(
                opportunity=opp,
                standardized_8h_net_funding_bps=Decimal("0"),
                expected_total_funding_usdt=Decimal("0"),
                round_trip_fees_and_slippage_usdt=Decimal("0"),
                break_even_settlements=Decimal("999"),
                expected_net_carry_usdt=Decimal("0"),
                net_carry_bps=Decimal("0"),
            )

        # Compute standardized 8h net funding rate (`S2-006`)
        # Short perp receives positive `short_funding`; if long leg is also a perp, it pays `long_funding`
        short_8h_bps = short_funding.standardized_8h_funding_bps
        long_8h_bps = long_funding.standardized_8h_funding_bps if long_funding else Decimal("0")
        net_8h_funding_bps = short_8h_bps - long_8h_bps

        notional_usdt = short_sim.quote_notional
        per_settlement_funding_usdt = notional_usdt * (net_8h_funding_bps / Decimal("10000"))
        expected_total_funding_usdt = per_settlement_funding_usdt * Decimal(str(spec.expected_holding_settlements))

        # Round-trip open + close fees & entry spread/slippage cost (`S2-008`)
        long_fee = self._cost_engine.estimate_fees(spec.long_venue_id, long_sim.quote_notional, "USDT", is_maker=False)
        short_fee = self._cost_engine.estimate_fees(spec.short_deriv_venue_id, short_sim.quote_notional, "USDT", is_maker=False)
        round_trip_fees_usdt = (long_fee.fee_in_settlement_asset + short_fee.fee_in_settlement_asset) * Decimal("2")

        # Entry spread cost if long ask > short bid
        entry_spread_cost = max(Decimal("0"), long_sim.quote_notional - short_sim.quote_notional)
        total_cost_usdt = round_trip_fees_usdt + entry_spread_cost

        break_even_settlements = (
            total_cost_usdt / per_settlement_funding_usdt
            if per_settlement_funding_usdt > Decimal("0")
            else Decimal("999")
        )
        expected_net_carry_usdt = expected_total_funding_usdt - total_cost_usdt
        net_carry_bps = (expected_net_carry_usdt / long_sim.quote_notional) * Decimal("10000")

        opp_legs = [
            OpportunityLeg(
                venue_id=spec.long_venue_id,
                instrument_id=spec.long_instrument_id,
                side=OrderSide.BUY,
                target_quantity=long_qty,
                executable_price=long_sim.vwap_fill_price,
                executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                available_depth_qty=long_sim.available_depth_qty,
                expected_fee=long_fee.fee_amount,
                fee_asset=long_fee.fee_asset,
                rules_version=long_rules.rules_version,
            ),
            OpportunityLeg(
                venue_id=spec.short_deriv_venue_id,
                instrument_id=spec.short_deriv_instrument_id,
                side=OrderSide.SELL,
                target_quantity=short_qty,
                executable_price=short_sim.vwap_fill_price,
                executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                available_depth_qty=short_sim.available_depth_qty,
                expected_fee=short_fee.fee_amount,
                fee_asset=short_fee.fee_asset,
                rules_version=short_rules.rules_version,
            ),
        ]

        opp = Opportunity(
            strategy_id=self.STRATEGY_ID,
            strategy_version=self.STRATEGY_VERSION,
            status=OpportunityStatus.DETECTED,
            legs=opp_legs,
            input_snapshot_ids=snap_ids,
            settlement_asset="USDT",
            gross_edge=expected_total_funding_usdt,
            fees=total_cost_usdt,
            funding_or_carry=expected_total_funding_usdt,
            expected_net_pnl=expected_net_carry_usdt,
            net_edge_bps=net_carry_bps,
            capacity=long_sim.quote_notional,
            expires_at=expires_at,
            decision=OpportunityDecision.PAPER,
            event_time_utc=ref_now,
            receive_time_utc=ref_now,
        )
        self._lifecycle.transition(opp, OpportunityStatus.VALIDATED, now_utc=ref_now)
        self._lifecycle.transition(opp, OpportunityStatus.COSTED, now_utc=ref_now)

        if net_8h_funding_bps <= Decimal("0") or expected_net_carry_usdt <= Decimal("0"):
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.NON_POSITIVE_NET_PNL.value],
            )
        elif net_carry_bps < self._risk_policy.min_net_edge_bps:
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.BELOW_MIN_NET_EDGE_BPS.value],
            )
        else:
            self._lifecycle.transition(opp, OpportunityStatus.RISK_CHECKED, now_utc=ref_now)
            self._lifecycle.transition(opp, OpportunityStatus.PAPER, now_utc=ref_now)

        persisted = self._lifecycle.register_and_persist(opp, now_utc=ref_now)
        return FundingCarryScanReport(
            opportunity=persisted,
            standardized_8h_net_funding_bps=net_8h_funding_bps,
            expected_total_funding_usdt=expected_total_funding_usdt,
            round_trip_fees_and_slippage_usdt=total_cost_usdt,
            break_even_settlements=break_even_settlements,
            expected_net_carry_usdt=expected_net_carry_usdt,
            net_carry_bps=net_carry_bps,
        )

    def _rejected(
        self,
        snap_ids: List[str],
        expires_at: datetime,
        ref_now: datetime,
        reasons: List[str],
    ) -> Opportunity:
        opp = Opportunity(
            strategy_id=self.STRATEGY_ID,
            strategy_version=self.STRATEGY_VERSION,
            status=OpportunityStatus.DETECTED,
            legs=[],
            input_snapshot_ids=snap_ids or ["missing"],
            settlement_asset="USDT",
            gross_edge=Decimal("0"),
            fees=Decimal("0"),
            expected_net_pnl=Decimal("-1"),
            net_edge_bps=Decimal("-100"),
            capacity=Decimal("0"),
            expires_at=expires_at,
            decision=OpportunityDecision.REJECT,
            event_time_utc=ref_now,
            receive_time_utc=ref_now,
        )
        self._lifecycle.transition(opp, OpportunityStatus.REJECTED, now_utc=ref_now, reject_reasons=reasons)
        return self._lifecycle.register_and_persist(opp, now_utc=ref_now)
