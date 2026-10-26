"""DEX-004: S4-A CEX-DEX Cross-Market Arbitrage Scanner & Non-Atomic Leg Recovery Planner.

Enforces S4-A acceptance gates (`S4A-001` through `S4A-008`):
- Requires healthy RPC quorum and fresh block header (`DEX-001`), whitelisted pool & tax-free
  tokens (`S4A-002`), and valid `simulate_swap` output (`DEX-002`, `S4A-001`).
- Deducts CEX taker fee + CEX L2 depth slippage + DEX LP fee + AMM price impact + EIP-1559
  gas cost in USD + MEV risk budget (`S4A-003`).
- Never assumes CEX and DEX legs execute atomically (`S4A-007`): every accepted candidate
  includes a concrete `CexDexRecoveryPlan` defining how to unwind or compensate when one leg
  succeeds and the other leg reverts or times out.
- Hard-locked to `OpportunityDecision.PAPER` prior to live approval (`S4A-008`).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.dex.chain import BlockHeaderSnapshot, EvmChainAdapter
from arb.dex.pool_sim import AmmPoolState, DexPoolSimulator, SwapSimulationResult
from arb.domain.models import (
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderBook,
    OrderSide,
    PriceType,
)


class CexDexRecoveryPlan(BaseModel):
    """Explicit non-atomic recovery plan for a 2-leg CEX <-> DEX arbitrage cycle (`S4A-007`)."""

    primary_leg_venue: str
    secondary_leg_venue: str
    unwind_cex_on_dex_revert: bool = True
    fallback_cex_compensating_side: OrderSide
    max_unhedged_window_ms: int = 15000
    mev_private_relay_required: bool = True
    recovery_slippage_budget_bps: Decimal = Decimal("25.0")

    class Config:
        frozen = True


class CexDexArbEvaluation(BaseModel):
    accepted: bool
    rejection_reason: Optional[str] = None
    direction: str  # "BUY_DEX_SELL_CEX" | "BUY_CEX_SELL_DEX"
    base_asset: str
    quote_asset: str
    quantity_base: Decimal = Decimal("0")
    cex_vwap_price: Optional[Decimal] = None
    dex_execution_price: Optional[Decimal] = None
    dex_mid_price: Optional[Decimal] = None
    gross_edge_usd: Decimal = Decimal("0")
    cex_fee_usd: Decimal = Decimal("0")
    dex_gas_cost_usd: Decimal = Decimal("0")
    mev_risk_budget_usd: Decimal = Decimal("0")
    expected_net_pnl_usd: Decimal = Decimal("0")
    net_edge_bps: Decimal = Decimal("0")
    state_block_number: int = 0
    state_block_hash: str = ""
    swap_simulation: Optional[SwapSimulationResult] = None
    recovery_plan: Optional[CexDexRecoveryPlan] = None
    candidate: Optional[Opportunity] = None

    class Config:
        frozen = True


class CexDexArbitrageScanner:
    """DEX-004: Scans CEX L2 order book vs. on-chain DEX AMM pool simulation and builds recovery plans."""

    strategy_id: str = "strat_s4a_cex_dex"
    strategy_version: str = "1.0.0"

    def __init__(
        self,
        chain_adapter: EvmChainAdapter,
        pool_simulator: DexPoolSimulator,
        min_net_edge_bps: Decimal = Decimal("10.0"),
        cex_taker_fee_bps: Decimal = Decimal("8.0"),
    ) -> None:
        self.chain_adapter = chain_adapter
        self.pool_simulator = pool_simulator
        self.min_net_edge_bps = min_net_edge_bps
        self.cex_taker_fee_bps = cex_taker_fee_bps

    @staticmethod
    def _estimate_cex_vwap(book: OrderBook, side: OrderSide, target_qty: Decimal) -> Tuple[bool, Optional[Decimal], Decimal]:
        if not book.is_valid:
            return False, None, Decimal("0")
        levels = book.asks if side == OrderSide.BUY else book.bids
        remaining = target_qty
        filled = Decimal("0")
        notional = Decimal("0")
        for lvl in levels:
            if remaining <= Decimal("0"):
                break
            take = min(remaining, lvl.quantity)
            filled += take
            notional += take * lvl.price
            remaining -= take
        if remaining > Decimal("0") or filled <= Decimal("0"):
            return False, None, filled
        return True, notional / filled, filled

    def evaluate_cex_dex_pair(
        self,
        cex_book: OrderBook,
        pool_state: AmmPoolState,
        base_asset_symbol: str,
        quote_asset_symbol: str,
        target_base_qty: Decimal,
        latest_block: BlockHeaderSnapshot,
        native_gas_token_usd_price: Decimal,
        now_utc: datetime,
    ) -> CexDexArbEvaluation:
        """
        Evaluate both `BUY_DEX_SELL_CEX` and `BUY_CEX_SELL_DEX` directions using exact AMM simulation
        and CEX L2 depth walk.
        """
        base = base_asset_symbol.upper()
        quote = quote_asset_symbol.upper()

        # 1. Check RPC quorum & block freshness (DEX-001)
        rpc_health = self.chain_adapter.check_rpc_quorum_and_health(now_utc)
        if not rpc_health.is_healthy:
            return CexDexArbEvaluation(
                accepted=False,
                rejection_reason="CHAIN_RPC_UNHEALTHY:" + ",".join(rpc_health.reason_codes),
                direction="NONE",
                base_asset=base,
                quote_asset=quote,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
            )

        # 2. Check CEX L2 depth for both BUY and SELL
        ok_cex_sell, cex_bid_vwap, avail_cex_bid = self._estimate_cex_vwap(cex_book, OrderSide.SELL, target_base_qty)
        ok_cex_buy, cex_ask_vwap, avail_cex_ask = self._estimate_cex_vwap(cex_book, OrderSide.BUY, target_base_qty)
        if not ok_cex_sell and not ok_cex_buy:
            return CexDexArbEvaluation(
                accepted=False,
                rejection_reason="CEX_INSUFFICIENT_L2_DEPTH",
                direction="NONE",
                base_asset=base,
                quote_asset=quote,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
            )

        # 3. Direction A: SELL base on DEX (token_in = base -> token_out = quote) & BUY base on CEX (@ cex_ask_vwap)
        notional_est = (cex_ask_vwap or cex_bid_vwap or Decimal("1")) * target_base_qty
        sim_sell_dex = self.pool_simulator.simulate_swap(
            pool_state=pool_state,
            token_in_symbol=base,
            amount_in=target_base_qty,
            latest_block=latest_block,
            native_gas_token_usd_price=native_gas_token_usd_price,
            notional_usd_estimate=notional_est,
            now_utc=now_utc,
        )
        if not sim_sell_dex.is_valid:
            return CexDexArbEvaluation(
                accepted=False,
                rejection_reason=sim_sell_dex.reject_reason or "DEX_SIMULATION_FAILED",
                direction="BUY_CEX_SELL_DEX",
                base_asset=base,
                quote_asset=quote,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                swap_simulation=sim_sell_dex,
            )

        # Direction B: BUY base on DEX (spend quote_in = target_base_qty * pool_mid to buy base) & SELL base on CEX (@ cex_bid_vwap)
        # Check which direction yields higher gross edge:
        # In Direction A (BUY CEX @ cex_ask_vwap, SELL DEX @ sim_sell_dex.execution_price):
        gross_a = (
            (sim_sell_dex.expected_amount_out - (cex_ask_vwap * target_base_qty))
            if (ok_cex_buy and cex_ask_vwap is not None)
            else Decimal("-999999")
        )

        # In Direction B (BUY DEX by spending quote_in = cex_bid_vwap * target_base_qty, SELL CEX @ cex_bid_vwap):
        quote_in_for_b = (cex_bid_vwap * target_base_qty) if cex_bid_vwap else notional_est
        sim_buy_dex = self.pool_simulator.simulate_swap(
            pool_state=pool_state,
            token_in_symbol=quote,
            amount_in=quote_in_for_b,
            latest_block=latest_block,
            native_gas_token_usd_price=native_gas_token_usd_price,
            notional_usd_estimate=notional_est,
            now_utc=now_utc,
        )
        # When spending quote_in_for_b on DEX, we receive `sim_buy_dex.expected_amount_out` of base,
        # which can be sold on CEX at `cex_bid_vwap`:
        gross_b = (
            ((sim_buy_dex.expected_amount_out * cex_bid_vwap) - quote_in_for_b)
            if (sim_buy_dex.is_valid and ok_cex_sell and cex_bid_vwap is not None)
            else Decimal("-999999")
        )

        if gross_a >= gross_b:
            direction = "BUY_CEX_SELL_DEX"
            chosen_sim = sim_sell_dex
            cex_side = OrderSide.BUY
            cex_vwap = cex_ask_vwap or Decimal("0")
            cex_avail = avail_cex_ask
            dex_exec_px = sim_sell_dex.execution_price
            dex_mid_px = sim_sell_dex.mid_price
            gross_edge_usd = gross_a
            capital_usd = cex_vwap * target_base_qty
            fallback_cex_side = OrderSide.SELL
        else:
            direction = "BUY_DEX_SELL_CEX"
            chosen_sim = sim_buy_dex
            cex_side = OrderSide.SELL
            cex_vwap = cex_bid_vwap or Decimal("0")
            cex_avail = avail_cex_bid
            dex_exec_px = (
                quote_in_for_b / sim_buy_dex.expected_amount_out
                if sim_buy_dex.expected_amount_out > Decimal("0")
                else Decimal("0")
            )
            dex_mid_px = (
                Decimal("1") / sim_buy_dex.mid_price
                if sim_buy_dex.mid_price > Decimal("0")
                else Decimal("0")
            )
            gross_edge_usd = gross_b
            capital_usd = quote_in_for_b
            fallback_cex_side = OrderSide.BUY

        cex_fee_usd = (capital_usd * self.cex_taker_fee_bps) / Decimal("10000")
        gas_usd = chosen_sim.estimated_gas_cost_usd
        mev_usd = chosen_sim.mev_risk_budget_usd
        total_costs_usd = cex_fee_usd + gas_usd + mev_usd
        net_pnl_usd = gross_edge_usd - total_costs_usd
        net_edge_bps = (
            (net_pnl_usd / capital_usd) * Decimal("10000")
            if capital_usd > Decimal("0")
            else Decimal("-9999")
        )

        recovery_plan = CexDexRecoveryPlan(
            primary_leg_venue=f"dex_chain_{pool_state.chain_id}",
            secondary_leg_venue=cex_book.venue_id,
            unwind_cex_on_dex_revert=True,
            fallback_cex_compensating_side=fallback_cex_side,
            max_unhedged_window_ms=15000,
            mev_private_relay_required=True,
        )

        if net_pnl_usd <= Decimal("0") or net_edge_bps < self.min_net_edge_bps:
            return CexDexArbEvaluation(
                accepted=False,
                rejection_reason="INSUFFICIENT_NET_EDGE_AFTER_GAS_MEV_AND_IMPACT",
                direction=direction,
                base_asset=base,
                quote_asset=quote,
                quantity_base=target_base_qty,
                cex_vwap_price=cex_vwap,
                dex_execution_price=dex_exec_px,
                dex_mid_price=dex_mid_px,
                gross_edge_usd=gross_edge_usd,
                cex_fee_usd=cex_fee_usd,
                dex_gas_cost_usd=gas_usd,
                mev_risk_budget_usd=mev_usd,
                expected_net_pnl_usd=net_pnl_usd,
                net_edge_bps=net_edge_bps,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                swap_simulation=chosen_sim,
                recovery_plan=recovery_plan,
            )

        candidate = Opportunity(
            opportunity_id=f"opp_s4a_dex_{uuid4().hex[:12]}",
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            status=OpportunityStatus.PAPER,
            legs=[
                OpportunityLeg(
                    venue_id=cex_book.venue_id,
                    instrument_id=cex_book.instrument_id,
                    side=cex_side,
                    target_quantity=target_base_qty,
                    executable_price=cex_vwap,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=cex_avail,
                    expected_fee=cex_fee_usd,
                    fee_asset=quote,
                    rules_version="v1",
                ),
                OpportunityLeg(
                    venue_id=f"dex_chain_{pool_state.chain_id}",
                    instrument_id=pool_state.pool_address,
                    side=OrderSide.SELL if cex_side == OrderSide.BUY else OrderSide.BUY,
                    target_quantity=target_base_qty,
                    executable_price=dex_exec_px,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=pool_state.reserve0,
                    expected_fee=gas_usd + mev_usd,
                    fee_asset=quote,
                    rules_version=f"block_{pool_state.block_number}",
                ),
            ],
            input_snapshot_ids=[
                f"{cex_book.venue_id}:{cex_book.instrument_id}:{cex_book.last_update_id}",
                f"chain_{pool_state.chain_id}:{pool_state.pool_address}:{pool_state.block_number}",
            ],
            settlement_asset=quote,
            gross_edge=gross_edge_usd,
            fees=cex_fee_usd,
            gas_or_bridge_cost=gas_usd + mev_usd,
            expected_net_pnl=net_pnl_usd,
            net_edge_bps=net_edge_bps,
            capacity=capital_usd,
            expires_at=now_utc + timedelta(seconds=12),
            decision=OpportunityDecision.PAPER,
            event_time_utc=now_utc,
            receive_time_utc=now_utc,
        )

        return CexDexArbEvaluation(
            accepted=True,
            rejection_reason=None,
            direction=direction,
            base_asset=base,
            quote_asset=quote,
            quantity_base=target_base_qty,
            cex_vwap_price=cex_vwap,
            dex_execution_price=dex_exec_px,
            dex_mid_price=dex_mid_px,
            gross_edge_usd=gross_edge_usd,
            cex_fee_usd=cex_fee_usd,
            dex_gas_cost_usd=gas_usd,
            mev_risk_budget_usd=mev_usd,
            expected_net_pnl_usd=net_pnl_usd,
            net_edge_bps=net_edge_bps,
            state_block_number=pool_state.block_number,
            state_block_hash=pool_state.block_hash,
            swap_simulation=chosen_sim,
            recovery_plan=recovery_plan,
            candidate=candidate,
        )
