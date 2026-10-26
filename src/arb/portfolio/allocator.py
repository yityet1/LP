"""PORT-001: Portfolio-Level Capital Allocation, Exposure Aggregation, Concentration & Cost Attribution.

Enforces Portfolio acceptance gates (`PORT-001` through `PORT-004`):
- Prevents double-allocation of the same free balance across multiple concurrent strategies (`PORT-001`).
- Excludes locked, reserved, margin-encumbered, and in-transit bridge funds from free allocatable
  capital (`PORT-002`).
- Enforces strategy, venue, and asset concentration limits across all active strategies (`PORT-003`).
- Tracks granular PnL and cost attribution by `strategy_id` (`gross_edge`, `trading_fees`,
  `funding_or_borrow`, `gas_or_bridge`, `slippage_or_haircut`, `net_pnl`) (`PORT-004`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.domain.models import BalanceSnapshot, Opportunity


class StrategyCostAttributionRecord(BaseModel):
    strategy_id: str
    cycles_count: int = 0
    gross_edge_usd: Decimal = Decimal("0")
    trading_fees_usd: Decimal = Decimal("0")
    funding_or_borrow_cost_usd: Decimal = Decimal("0")
    gas_or_bridge_cost_usd: Decimal = Decimal("0")
    slippage_and_haircut_usd: Decimal = Decimal("0")
    net_realized_pnl_usd: Decimal = Decimal("0")


class PortfolioExposureSummary(BaseModel):
    total_portfolio_notional_usd: Decimal
    notional_by_strategy: Dict[str, Decimal]
    notional_by_venue: Dict[str, Decimal]
    notional_by_asset: Dict[str, Decimal]
    total_free_capital_usd: Decimal
    total_reserved_capital_usd: Decimal
    total_locked_or_margin_usd: Decimal
    total_in_transit_usd: Decimal


class CapitalAllocationDecision(BaseModel):
    allocation_id: str = Field(default_factory=lambda: f"alloc_{uuid4().hex[:10]}")
    opportunity_id: str
    strategy_id: str
    venue_id: str
    asset: str
    requested_notional_usd: Decimal
    approved: bool
    rejection_reason: Optional[str] = None
    evaluated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PortfolioCapitalAllocator:
    """PORT-001: Central portfolio capital allocator, concentration guard, and PnL/cost attribution engine."""

    def __init__(
        self,
        max_portfolio_notional_usd: Decimal = Decimal("25000.0"),
        max_strategy_notional_usd: Decimal = Decimal("10000.0"),
        max_venue_notional_usd: Decimal = Decimal("15000.0"),
        max_asset_concentration_usd: Decimal = Decimal("12000.0"),
    ) -> None:
        self.max_portfolio_notional_usd = max_portfolio_notional_usd
        self.max_strategy_notional_usd = max_strategy_notional_usd
        self.max_venue_notional_usd = max_venue_notional_usd
        self.max_asset_concentration_usd = max_asset_concentration_usd

        self._balances: Dict[Tuple[str, str], BalanceSnapshot] = {}
        self._allocated_by_venue_asset: Dict[Tuple[str, str], Decimal] = {}
        self._notional_by_strategy: Dict[str, Decimal] = {}
        self._notional_by_venue: Dict[str, Decimal] = {}
        self._notional_by_asset: Dict[str, Decimal] = {}
        self._attribution: Dict[str, StrategyCostAttributionRecord] = {}
        self._isolated_halted_strategies: Dict[str, str] = {}

    def update_balance_snapshot(self, snapshot: BalanceSnapshot) -> None:
        self._balances[(snapshot.venue_id, snapshot.asset.upper())] = snapshot

    def isolate_and_halt_strategy(self, strategy_id: str, reason: str) -> None:
        """Isolate a faulty strategy without halting healthy independent strategies."""
        self._isolated_halted_strategies[strategy_id] = reason

    def is_strategy_isolated(self, strategy_id: str) -> Tuple[bool, Optional[str]]:
        if strategy_id in self._isolated_halted_strategies:
            return True, self._isolated_halted_strategies[strategy_id]
        return False, None

    def request_allocation(
        self,
        opportunity_id: str,
        strategy_id: str,
        venue_id: str,
        asset: str,
        requested_amount: Decimal,
        requested_notional_usd: Decimal,
        reduce_only: bool = False,
    ) -> CapitalAllocationDecision:
        """
        Atomically check strategy isolation, free allocatable balance (excluding locked, reserved,
        margin, and in-transit funds), and strategy/venue/asset/portfolio concentration caps.
        """
        asset_u = asset.upper()
        halted, halt_reason = self.is_strategy_isolated(strategy_id)
        if halted and not reduce_only:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason=f"STRATEGY_ISOLATED_HALTED:{halt_reason}",
            )

        if reduce_only:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=True,
            )

        snap = self._balances.get((venue_id, asset_u))
        if snap is None:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason=f"MISSING_VENUE_BALANCE_SNAPSHOT:{venue_id}:{asset_u}",
            )

        already_alloc = self._allocated_by_venue_asset.get((venue_id, asset_u), Decimal("0"))
        # Note: snap.allocatable_balance strictly excludes locked_balance, reserved_balance, and in_transit_balance!
        remaining_free = snap.allocatable_balance - already_alloc
        if requested_amount > remaining_free:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason=(
                    f"INSUFFICIENT_ALLOCATABLE_CAPITAL:{venue_id}:{asset_u}:"
                    f"req={requested_amount}>avail={remaining_free}(in_transit={snap.in_transit_balance})"
                ),
            )

        cur_strat = self._notional_by_strategy.get(strategy_id, Decimal("0"))
        if cur_strat + requested_notional_usd > self.max_strategy_notional_usd:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason="STRATEGY_CONCENTRATION_LIMIT_EXCEEDED",
            )

        cur_venue = self._notional_by_venue.get(venue_id, Decimal("0"))
        if cur_venue + requested_notional_usd > self.max_venue_notional_usd:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason="VENUE_CONCENTRATION_LIMIT_EXCEEDED",
            )

        cur_asset = self._notional_by_asset.get(asset_u, Decimal("0"))
        if cur_asset + requested_notional_usd > self.max_asset_concentration_usd:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason="ASSET_CONCENTRATION_LIMIT_EXCEEDED",
            )

        total_port = sum(self._notional_by_strategy.values(), Decimal("0"))
        if total_port + requested_notional_usd > self.max_portfolio_notional_usd:
            return CapitalAllocationDecision(
                opportunity_id=opportunity_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset_u,
                requested_notional_usd=requested_notional_usd,
                approved=False,
                rejection_reason="PORTFOLIO_TOTAL_NOTIONAL_LIMIT_EXCEEDED",
            )

        self._allocated_by_venue_asset[(venue_id, asset_u)] = already_alloc + requested_amount
        self._notional_by_strategy[strategy_id] = cur_strat + requested_notional_usd
        self._notional_by_venue[venue_id] = cur_venue + requested_notional_usd
        self._notional_by_asset[asset_u] = cur_asset + requested_notional_usd

        return CapitalAllocationDecision(
            opportunity_id=opportunity_id,
            strategy_id=strategy_id,
            venue_id=venue_id,
            asset=asset_u,
            requested_notional_usd=requested_notional_usd,
            approved=True,
        )

    def record_strategy_attribution(self, opp: Opportunity) -> StrategyCostAttributionRecord:
        rec = self._attribution.get(opp.strategy_id) or StrategyCostAttributionRecord(strategy_id=opp.strategy_id)
        updated = StrategyCostAttributionRecord(
            strategy_id=opp.strategy_id,
            cycles_count=rec.cycles_count + 1,
            gross_edge_usd=rec.gross_edge_usd + opp.gross_edge,
            trading_fees_usd=rec.trading_fees_usd + opp.fees,
            funding_or_borrow_cost_usd=rec.funding_or_borrow_cost_usd + opp.funding_or_carry,
            gas_or_bridge_cost_usd=rec.gas_or_bridge_cost_usd + opp.gas_or_bridge_cost,
            slippage_and_haircut_usd=rec.slippage_and_haircut_usd + opp.slippage,
            net_realized_pnl_usd=rec.net_realized_pnl_usd + opp.expected_net_pnl,
        )
        self._attribution[opp.strategy_id] = updated
        return updated

    def get_exposure_summary(self) -> PortfolioExposureSummary:
        free_usd = Decimal("0")
        reserved_usd = Decimal("0")
        locked_usd = Decimal("0")
        transit_usd = Decimal("0")
        for (_vid, _asset), snap in self._balances.items():
            alloc = self._allocated_by_venue_asset.get((_vid, _asset), Decimal("0"))
            free_usd += max(Decimal("0"), snap.allocatable_balance - alloc)
            reserved_usd += snap.reserved_balance + alloc
            locked_usd += snap.locked_balance
            transit_usd += snap.in_transit_balance

        return PortfolioExposureSummary(
            total_portfolio_notional_usd=sum(self._notional_by_strategy.values(), Decimal("0")),
            notional_by_strategy=dict(self._notional_by_strategy),
            notional_by_venue=dict(self._notional_by_venue),
            notional_by_asset=dict(self._notional_by_asset),
            total_free_capital_usd=free_usd,
            total_reserved_capital_usd=reserved_usd,
            total_locked_or_margin_usd=locked_usd,
            total_in_transit_usd=transit_usd,
        )
