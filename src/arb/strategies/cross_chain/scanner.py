"""XCH-002: S4-C Cross-Chain & Wrapped Asset Price Discrepancy Scanner with Transit-Time Risk Haircut.

Enforces S4-C acceptance gates (`S4C-001` through `S4C-006`):
- Never treats cross-chain bridging as an instantaneous operation (`S4C-004`).
- In-transit bridged balances are NEVER counted as available destination inventory for
  immediate execution (`S4C-001`, `S4C-002`).
- Deducts source execution cost + destination execution cost + fixed/variable bridge fees +
  wrapped asset peg discount + transit-time price volatility haircut
  (`volatility_bps_per_sqrt_min * sqrt(transit_minutes)`).
- Only candidates whose risk-adjusted net edge exceeds `min_risk_adjusted_net_edge_bps` enter
  the `PAPER` candidate pool (`S4C-005`, `S4C-006`).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import math
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel

from arb.bridge.ledger import BridgeTransferLedger
from arb.domain.models import (
    BalanceSnapshot,
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderSide,
    PriceType,
)


class CrossChainArbEvaluation(BaseModel):
    accepted: bool
    rejection_reason: Optional[str] = None
    route_id: str
    source_chain_venue: str
    destination_chain_venue: str
    asset_symbol: str
    quantity: Decimal
    source_buy_price: Decimal
    destination_sell_price: Decimal
    effective_destination_price_after_peg: Decimal
    expected_transit_seconds: int
    gross_edge_usd: Decimal
    trading_and_gas_fees_usd: Decimal
    bridge_fee_usd: Decimal
    transit_volatility_haircut_usd: Decimal
    wrapped_depeg_haircut_usd: Decimal
    risk_adjusted_net_pnl_usd: Decimal
    risk_adjusted_net_edge_bps: Decimal
    requires_prepositioned_destination_inventory: bool = True
    candidate: Optional[Opportunity] = None

    class Config:
        frozen = True


class CrossChainArbitrageScanner:
    """XCH-002: Evaluates cross-chain spread opportunities with explicit transit-time and peg haircuts."""

    strategy_id: str = "strat_s4c_cross_chain"
    strategy_version: str = "1.0.0"

    def __init__(
        self,
        bridge_ledger: BridgeTransferLedger,
        min_risk_adjusted_net_edge_bps: Decimal = Decimal("15.0"),
        source_swap_and_gas_bps: Decimal = Decimal("12.0"),
        dest_swap_and_gas_bps: Decimal = Decimal("12.0"),
        volatility_haircut_bps_per_sqrt_min: Decimal = Decimal("8.0"),
    ) -> None:
        self.bridge_ledger = bridge_ledger
        self.min_risk_adjusted_net_edge_bps = min_risk_adjusted_net_edge_bps
        self.source_swap_and_gas_bps = source_swap_and_gas_bps
        self.dest_swap_and_gas_bps = dest_swap_and_gas_bps
        self.volatility_haircut_bps_per_sqrt_min = volatility_haircut_bps_per_sqrt_min

    def evaluate_cross_chain_spread(
        self,
        route_id: str,
        source_chain_venue: str,
        destination_chain_venue: str,
        asset_symbol: str,
        quantity: Decimal,
        source_executable_ask: Decimal,
        destination_executable_bid: Decimal,
        destination_balance_snapshot: BalanceSnapshot,
        require_immediate_dest_inventory: bool,
        now_utc: datetime,
    ) -> CrossChainArbEvaluation:
        # 1. Check bridge route health, timeout pause, and wrapped asset peg ratio (`S4C-003`)
        paused, reason = self.bridge_ledger.is_route_paused(route_id)
        route = self.bridge_ledger.get_route(route_id)
        if paused or route is None:
            return CrossChainArbEvaluation(
                accepted=False,
                rejection_reason=reason or "BRIDGE_ROUTE_UNAVAILABLE",
                route_id=route_id,
                source_chain_venue=source_chain_venue,
                destination_chain_venue=destination_chain_venue,
                asset_symbol=asset_symbol,
                quantity=quantity,
                source_buy_price=source_executable_ask,
                destination_sell_price=destination_executable_bid,
                effective_destination_price_after_peg=destination_executable_bid,
                expected_transit_seconds=route.expected_transit_seconds if route else 0,
                gross_edge_usd=Decimal("0"),
                trading_and_gas_fees_usd=Decimal("0"),
                bridge_fee_usd=Decimal("0"),
                transit_volatility_haircut_usd=Decimal("0"),
                wrapped_depeg_haircut_usd=Decimal("0"),
                risk_adjusted_net_pnl_usd=Decimal("0"),
                risk_adjusted_net_edge_bps=Decimal("0"),
            )

        # 2. Enforce that in-transit balance is NEVER counted as free destination inventory (`S4C-001`, `S4C-002`)
        if require_immediate_dest_inventory and destination_balance_snapshot.allocatable_balance < quantity:
            return CrossChainArbEvaluation(
                accepted=False,
                rejection_reason=(
                    f"INSUFFICIENT_SETTLED_DEST_INVENTORY_IN_TRANSIT_EXCLUDED:"
                    f"allocatable={destination_balance_snapshot.allocatable_balance}<{quantity}"
                    f"(in_transit={destination_balance_snapshot.in_transit_balance})"
                ),
                route_id=route_id,
                source_chain_venue=source_chain_venue,
                destination_chain_venue=destination_chain_venue,
                asset_symbol=asset_symbol,
                quantity=quantity,
                source_buy_price=source_executable_ask,
                destination_sell_price=destination_executable_bid,
                effective_destination_price_after_peg=destination_executable_bid * route.wrapped_peg_ratio_to_canonical,
                expected_transit_seconds=route.expected_transit_seconds,
                gross_edge_usd=Decimal("0"),
                trading_and_gas_fees_usd=Decimal("0"),
                bridge_fee_usd=Decimal("0"),
                transit_volatility_haircut_usd=Decimal("0"),
                wrapped_depeg_haircut_usd=Decimal("0"),
                risk_adjusted_net_pnl_usd=Decimal("0"),
                risk_adjusted_net_edge_bps=Decimal("0"),
            )

        # 3. Compute gross spread, bridge fees, wrapped peg discount, and transit-time volatility haircut (`S4C-004`, `S4C-005`)
        notional_usd = source_executable_ask * quantity
        eff_dest_px = destination_executable_bid * route.wrapped_peg_ratio_to_canonical
        gross_edge_usd = (destination_executable_bid - source_executable_ask) * quantity
        wrapped_depeg_haircut_usd = (destination_executable_bid - eff_dest_px) * quantity

        trading_gas_usd = (
            notional_usd * (self.source_swap_and_gas_bps + self.dest_swap_and_gas_bps)
        ) / Decimal("10000")
        bridge_fee_usd = route.fixed_bridge_fee_usd + (
            (notional_usd * route.variable_bridge_fee_bps) / Decimal("10000")
        )

        transit_minutes = max(1.0, route.expected_transit_seconds / 60.0)
        vol_haircut_bps = self.volatility_haircut_bps_per_sqrt_min * Decimal(f"{math.sqrt(transit_minutes):.4f}")
        transit_vol_haircut_usd = (notional_usd * vol_haircut_bps) / Decimal("10000")

        risk_adjusted_net_pnl_usd = (
            gross_edge_usd
            - trading_gas_usd
            - bridge_fee_usd
            - wrapped_depeg_haircut_usd
            - transit_vol_haircut_usd
        )
        risk_adjusted_net_edge_bps = (
            (risk_adjusted_net_pnl_usd / notional_usd) * Decimal("10000")
            if notional_usd > Decimal("0")
            else Decimal("-9999")
        )

        if risk_adjusted_net_pnl_usd <= Decimal("0") or risk_adjusted_net_edge_bps < self.min_risk_adjusted_net_edge_bps:
            return CrossChainArbEvaluation(
                accepted=False,
                rejection_reason="INSUFFICIENT_RISK_ADJUSTED_NET_EDGE_AFTER_BRIDGE_AND_TRANSIT_HAIRCUT",
                route_id=route_id,
                source_chain_venue=source_chain_venue,
                destination_chain_venue=destination_chain_venue,
                asset_symbol=asset_symbol,
                quantity=quantity,
                source_buy_price=source_executable_ask,
                destination_sell_price=destination_executable_bid,
                effective_destination_price_after_peg=eff_dest_px,
                expected_transit_seconds=route.expected_transit_seconds,
                gross_edge_usd=gross_edge_usd,
                trading_and_gas_fees_usd=trading_gas_usd,
                bridge_fee_usd=bridge_fee_usd,
                transit_volatility_haircut_usd=transit_vol_haircut_usd,
                wrapped_depeg_haircut_usd=wrapped_depeg_haircut_usd,
                risk_adjusted_net_pnl_usd=risk_adjusted_net_pnl_usd,
                risk_adjusted_net_edge_bps=risk_adjusted_net_edge_bps,
            )

        candidate = Opportunity(
            opportunity_id=f"opp_s4c_xch_{uuid4().hex[:12]}",
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            status=OpportunityStatus.PAPER,
            legs=[
                OpportunityLeg(
                    venue_id=source_chain_venue,
                    instrument_id=f"{asset_symbol}-USDT",
                    side=OrderSide.BUY,
                    target_quantity=quantity,
                    executable_price=source_executable_ask,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=quantity * Decimal("5"),
                    expected_fee=trading_gas_usd * Decimal("0.5"),
                    fee_asset="USDT",
                    rules_version=route_id,
                ),
                OpportunityLeg(
                    venue_id=destination_chain_venue,
                    instrument_id=f"{asset_symbol}-USDT",
                    side=OrderSide.SELL,
                    target_quantity=quantity,
                    executable_price=destination_executable_bid,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=destination_balance_snapshot.allocatable_balance,
                    expected_fee=trading_gas_usd * Decimal("0.5"),
                    fee_asset="USDT",
                    rules_version=route_id,
                ),
            ],
            input_snapshot_ids=[
                f"{source_chain_venue}:{asset_symbol}:{int(now_utc.timestamp())}",
                f"{destination_chain_venue}:{asset_symbol}:{int(now_utc.timestamp())}",
            ],
            settlement_asset="USDT",
            gross_edge=gross_edge_usd,
            fees=trading_gas_usd,
            slippage=wrapped_depeg_haircut_usd + transit_vol_haircut_usd,
            gas_or_bridge_cost=bridge_fee_usd,
            expected_net_pnl=risk_adjusted_net_pnl_usd,
            net_edge_bps=risk_adjusted_net_edge_bps,
            capacity=notional_usd,
            risk_score=vol_haircut_bps,
            expires_at=now_utc + timedelta(seconds=10),
            decision=OpportunityDecision.PAPER,
            event_time_utc=now_utc,
            receive_time_utc=now_utc,
        )

        return CrossChainArbEvaluation(
            accepted=True,
            rejection_reason=None,
            route_id=route_id,
            source_chain_venue=source_chain_venue,
            destination_chain_venue=destination_chain_venue,
            asset_symbol=asset_symbol,
            quantity=quantity,
            source_buy_price=source_executable_ask,
            destination_sell_price=destination_executable_bid,
            effective_destination_price_after_peg=eff_dest_px,
            expected_transit_seconds=route.expected_transit_seconds,
            gross_edge_usd=gross_edge_usd,
            trading_and_gas_fees_usd=trading_gas_usd,
            bridge_fee_usd=bridge_fee_usd,
            transit_volatility_haircut_usd=transit_vol_haircut_usd,
            wrapped_depeg_haircut_usd=wrapped_depeg_haircut_usd,
            risk_adjusted_net_pnl_usd=risk_adjusted_net_pnl_usd,
            risk_adjusted_net_edge_bps=risk_adjusted_net_edge_bps,
            requires_prepositioned_destination_inventory=require_immediate_dest_inventory,
            candidate=candidate,
        )
