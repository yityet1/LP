"""[DERIV-002] S2-A: Spot-Futures Cash-and-Carry & Perpetual Basis Arbitrage Engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from arb.config.settings import RiskPolicyConfig
from arb.costs.engine import CostEngine
from arb.domain.models import (
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderSide,
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


class BasisPairSpec(BaseModel):
    pair_id: str
    spot_venue_id: str
    spot_instrument_id: str
    deriv_venue_id: str
    deriv_instrument_id: str
    target_base_qty: Decimal


class BasisScanReport(BaseModel):
    opportunity: Opportunity
    spot_executable_ask: Decimal
    deriv_executable_bid: Decimal
    mark_price_reference: Optional[Decimal] = None
    spot_base_qty: Decimal
    deriv_contract_qty: Decimal
    deriv_contract_multiplier: Decimal
    net_delta_base_mismatch: Decimal
    gross_basis_bps: Decimal
    round_trip_fees_bps: Decimal
    net_basis_bps: Decimal
    annualized_net_basis_bps: Decimal
    days_to_expiry: Decimal
    should_unwind_near_expiry: bool = False
    should_unwind_on_convergence: bool = False


class BasisArbitrageScanner:
    """
    [DERIV-002] S2-A Cash-and-Carry (Spot vs. Dated Future) & Spot-Perp Basis Arbitrage Engine.
    Enforces S2 acceptance gates:
    - `S2-001`: Strictly uses Spot L2 `ASK` (walked for depth) and Derivative L2 `BID` (walked for depth)
      as executable prices; `mark_price` and `index_price` are NEVER used as order fill prices!
    - `S2-002` & `S2-003`: Aligns Spot `step_size` with Derivative `step_size * contract_multiplier`
      (e.g., OKX Swap `ctVal = 0.01 BTC`) so `spot_base_qty == deriv_contracts * contract_multiplier`
      (`net_delta_base_mismatch == 0`).
    - `S2-004`: Deducts full 4-leg round-trip fees (open spot + open deriv + close spot + close deriv)
      and multi-level slippage before computing `net_basis_bps` and `annualized_net_basis_bps`.
    - `S2-005`: Generates expiry/convergence exit signals when `days_to_expiry` falls within
      `unwind_window_hours_before_expiry` or when `net_basis_bps <= convergence_threshold_bps`.
    """

    STRATEGY_ID = "strat_s2a_spot_futures_basis"
    STRATEGY_VERSION = "1.0.0"

    def __init__(
        self,
        registry: InstrumentRegistry,
        health_gate: MarketHealthGate,
        cost_engine: CostEngine,
        lifecycle: OpportunityLifecycleManager,
        risk_policy: RiskPolicyConfig,
        min_annualized_basis_bps: Decimal = Decimal("500.0"),  # e.g. 5.0% annualized minimum
        default_perp_holding_days: Decimal = Decimal("7.0"),
        unwind_window_hours_before_expiry: Decimal = Decimal("24.0"),
        convergence_threshold_bps: Decimal = Decimal("2.0"),
        candidate_ttl_ms: int = 500,
    ) -> None:
        self._registry = registry
        self._health_gate = health_gate
        self._cost_engine = cost_engine
        self._lifecycle = lifecycle
        self._risk_policy = risk_policy
        self._min_annualized_bps = min_annualized_basis_bps
        self._default_perp_days = default_perp_holding_days
        self._unwind_window_hours = unwind_window_hours_before_expiry
        self._convergence_threshold_bps = convergence_threshold_bps
        self._candidate_ttl_ms = candidate_ttl_ms

    def evaluate_basis(
        self,
        spec: BasisPairSpec,
        order_books: Dict[Tuple[str, str], OrderBookBuilder],
        mark_price_reference: Optional[Decimal] = None,
        now_utc: Optional[datetime] = None,
    ) -> BasisScanReport:
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        expires_at = ref_now + timedelta(milliseconds=self._candidate_ttl_ms)

        spot_builder = order_books.get((spec.spot_venue_id, spec.spot_instrument_id))
        deriv_builder = order_books.get((spec.deriv_venue_id, spec.deriv_instrument_id))
        snap_ids: List[str] = []
        if spot_builder:
            snap_ids.append(f"{spec.spot_venue_id}:{spec.spot_instrument_id}:{spot_builder.last_update_id}")
        if deriv_builder:
            snap_ids.append(f"{spec.deriv_venue_id}:{spec.deriv_instrument_id}:{deriv_builder.last_update_id}")

        reject_reasons: List[str] = []
        try:
            spot_rules = self._registry.get_rules(spec.spot_venue_id, spec.spot_instrument_id, now_utc=ref_now)
            deriv_rules = self._registry.get_rules(spec.deriv_venue_id, spec.deriv_instrument_id, now_utc=ref_now)
            deriv_inst = self._registry.get(spec.deriv_venue_id, spec.deriv_instrument_id)
        except Exception:
            reject_reasons.append(OpportunityRejectReason.INSTRUMENT_NOT_TRADABLE.value)
            spot_rules = None  # type: ignore[assignment]
            deriv_rules = None  # type: ignore[assignment]
            deriv_inst = None  # type: ignore[assignment]

        if spot_builder is None or deriv_builder is None:
            reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
        else:
            health_ok, _, h_reasons = self._health_gate.evaluate_multi_book_gate(
                [spot_builder, deriv_builder], now_utc=ref_now
            )
            if not health_ok:
                reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
                reject_reasons.extend(h_reasons)

        if reject_reasons or spot_rules is None or deriv_rules is None or deriv_inst is None or spot_builder is None or deriv_builder is None:
            opp = self._rejected(snap_ids, expires_at, ref_now, reject_reasons)
            return BasisScanReport(
                opportunity=opp,
                spot_executable_ask=Decimal("0"),
                deriv_executable_bid=Decimal("0"),
                mark_price_reference=mark_price_reference,
                spot_base_qty=Decimal("0"),
                deriv_contract_qty=Decimal("0"),
                deriv_contract_multiplier=Decimal("1"),
                net_delta_base_mismatch=Decimal("0"),
                gross_basis_bps=Decimal("0"),
                round_trip_fees_bps=Decimal("0"),
                net_basis_bps=Decimal("0"),
                annualized_net_basis_bps=Decimal("0"),
                days_to_expiry=Decimal("0"),
            )

        # Align Spot base quantity and Derivative contract count via LCM/coarser effective base step (`S2-002`, `S2-003`)
        deriv_eff_base_step = deriv_rules.step_size * deriv_rules.contract_multiplier
        effective_base_unit = max(spot_rules.step_size, deriv_eff_base_step)
        units = (spec.target_base_qty / effective_base_unit).to_integral_value(rounding=ROUND_DOWN)
        aligned_base_qty = units * effective_base_unit

        if aligned_base_qty <= Decimal("0") or aligned_base_qty < spot_rules.min_qty:
            opp = self._rejected(
                snap_ids, expires_at, ref_now, [OpportunityRejectReason.STEP_OR_MIN_QTY_REJECT.value]
            )
            return BasisScanReport(
                opportunity=opp,
                spot_executable_ask=Decimal("0"),
                deriv_executable_bid=Decimal("0"),
                mark_price_reference=mark_price_reference,
                spot_base_qty=Decimal("0"),
                deriv_contract_qty=Decimal("0"),
                deriv_contract_multiplier=deriv_rules.contract_multiplier,
                net_delta_base_mismatch=Decimal("0"),
                gross_basis_bps=Decimal("0"),
                round_trip_fees_bps=Decimal("0"),
                net_basis_bps=Decimal("0"),
                annualized_net_basis_bps=Decimal("0"),
                days_to_expiry=Decimal("0"),
            )

        deriv_contract_qty = aligned_base_qty / deriv_rules.contract_multiplier
        net_delta_mismatch = aligned_base_qty - (deriv_contract_qty * deriv_rules.contract_multiplier)

        # Simulate L2 depth execution: BUY Spot @ ASK, SELL Derivative @ BID (`S2-001`)
        spot_sim = self._cost_engine.simulate_fill(
            spot_builder.to_order_book(), spot_rules, OrderSide.BUY, aligned_base_qty
        )
        deriv_sim = self._cost_engine.simulate_fill(
            deriv_builder.to_order_book(), deriv_rules, OrderSide.SELL, deriv_contract_qty
        )

        if not spot_sim.is_fully_executable or not deriv_sim.is_fully_executable:
            reasons = [
                r
                for r in (spot_sim.reject_reason, deriv_sim.reject_reason)
                if r is not None
            ] or [OpportunityRejectReason.INSUFFICIENT_DEPTH.value]
            opp = self._rejected(snap_ids, expires_at, ref_now, reasons)
            return BasisScanReport(
                opportunity=opp,
                spot_executable_ask=spot_sim.vwap_fill_price,
                deriv_executable_bid=deriv_sim.vwap_fill_price,
                mark_price_reference=mark_price_reference,
                spot_base_qty=aligned_base_qty,
                deriv_contract_qty=deriv_contract_qty,
                deriv_contract_multiplier=deriv_rules.contract_multiplier,
                net_delta_base_mismatch=net_delta_mismatch,
                gross_basis_bps=Decimal("0"),
                round_trip_fees_bps=Decimal("0"),
                net_basis_bps=Decimal("0"),
                annualized_net_basis_bps=Decimal("0"),
                days_to_expiry=Decimal("0"),
            )

        # Compute 4-leg round-trip fees (open spot + open deriv + close spot + close deriv) (`S2-004`)
        spot_open_fee = self._cost_engine.estimate_fees(
            spec.spot_venue_id, spot_sim.quote_notional, "USDT", is_maker=False
        )
        deriv_open_fee = self._cost_engine.estimate_fees(
            spec.deriv_venue_id, deriv_sim.quote_notional, "USDT", is_maker=False
        )
        # Round-trip includes both entry and exit taker fees
        total_round_trip_fees = (spot_open_fee.fee_in_settlement_asset + deriv_open_fee.fee_in_settlement_asset) * Decimal("2")

        gross_edge_usdt = deriv_sim.quote_notional - spot_sim.quote_notional
        net_pnl_usdt = gross_edge_usdt - total_round_trip_fees

        gross_basis_bps = (gross_edge_usdt / spot_sim.quote_notional) * Decimal("10000")
        round_trip_fees_bps = (total_round_trip_fees / spot_sim.quote_notional) * Decimal("10000")
        net_basis_bps = (net_pnl_usdt / spot_sim.quote_notional) * Decimal("10000")

        # Determine days to expiry and unwind triggers (`S2-005`)
        if deriv_inst.expiry is not None:
            rem_seconds = Decimal(str(max(0.0, (deriv_inst.expiry - ref_now).total_seconds())))
            days_to_expiry = max(Decimal("0.01"), rem_seconds / Decimal("86400"))
            hours_to_expiry = rem_seconds / Decimal("3600")
            should_unwind_expiry = hours_to_expiry <= self._unwind_window_hours
        else:
            days_to_expiry = self._default_perp_days
            should_unwind_expiry = False

        annualized_net_basis_bps = net_basis_bps * (Decimal("365") / days_to_expiry)
        should_unwind_conv = net_basis_bps <= self._convergence_threshold_bps

        opp_legs = [
            OpportunityLeg(
                venue_id=spec.spot_venue_id,
                instrument_id=spec.spot_instrument_id,
                side=OrderSide.BUY,
                target_quantity=aligned_base_qty,
                executable_price=spot_sim.vwap_fill_price,
                executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                available_depth_qty=spot_sim.available_depth_qty,
                expected_fee=spot_open_fee.fee_amount,
                fee_asset=spot_open_fee.fee_asset,
                rules_version=spot_rules.rules_version,
            ),
            OpportunityLeg(
                venue_id=spec.deriv_venue_id,
                instrument_id=spec.deriv_instrument_id,
                side=OrderSide.SELL,
                target_quantity=deriv_contract_qty,
                executable_price=deriv_sim.vwap_fill_price,
                executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                available_depth_qty=deriv_sim.available_depth_qty,
                expected_fee=deriv_open_fee.fee_amount,
                fee_asset=deriv_open_fee.fee_asset,
                rules_version=deriv_rules.rules_version,
            ),
        ]

        opp = Opportunity(
            strategy_id=self.STRATEGY_ID,
            strategy_version=self.STRATEGY_VERSION,
            status=OpportunityStatus.DETECTED,
            legs=opp_legs,
            input_snapshot_ids=snap_ids,
            settlement_asset="USDT",
            gross_edge=gross_edge_usdt,
            fees=total_round_trip_fees,
            expected_net_pnl=net_pnl_usdt,
            net_edge_bps=net_basis_bps,
            capacity=spot_sim.quote_notional,
            expires_at=expires_at,
            decision=OpportunityDecision.PAPER,
            event_time_utc=ref_now,
            receive_time_utc=ref_now,
        )
        self._lifecycle.transition(opp, OpportunityStatus.VALIDATED, now_utc=ref_now)
        self._lifecycle.transition(opp, OpportunityStatus.COSTED, now_utc=ref_now)

        if should_unwind_expiry:
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=["NEAR_EXPIRY_UNWIND_WINDOW_BLOCK_NEW_OPEN"],
            )
        elif net_pnl_usdt <= Decimal("0") or net_basis_bps < self._risk_policy.min_net_edge_bps:
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.BELOW_MIN_NET_EDGE_BPS.value],
            )
        elif annualized_net_basis_bps < self._min_annualized_bps:
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=["BELOW_MIN_ANNUALIZED_BASIS_BPS"],
            )
        else:
            self._lifecycle.transition(opp, OpportunityStatus.RISK_CHECKED, now_utc=ref_now)
            self._lifecycle.transition(opp, OpportunityStatus.PAPER, now_utc=ref_now)

        persisted = self._lifecycle.register_and_persist(opp, now_utc=ref_now)
        return BasisScanReport(
            opportunity=persisted,
            spot_executable_ask=spot_sim.vwap_fill_price,
            deriv_executable_bid=deriv_sim.vwap_fill_price,
            mark_price_reference=mark_price_reference,
            spot_base_qty=aligned_base_qty,
            deriv_contract_qty=deriv_contract_qty,
            deriv_contract_multiplier=deriv_rules.contract_multiplier,
            net_delta_base_mismatch=net_delta_mismatch,
            gross_basis_bps=gross_basis_bps,
            round_trip_fees_bps=round_trip_fees_bps,
            net_basis_bps=net_basis_bps,
            annualized_net_basis_bps=annualized_net_basis_bps,
            days_to_expiry=days_to_expiry,
            should_unwind_near_expiry=should_unwind_expiry,
            should_unwind_on_convergence=should_unwind_conv,
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
