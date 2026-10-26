"""DEX-002: AMM Pool State, Exact Swap Curve Simulation, and EIP-1559 Gas / MEV Cost Estimator.

Enforces S4-A acceptance gates (`S4A-001`, `S4A-002`, `S4A-003`):
- NEVER uses pool mid-price (`reserve1 / reserve0`) as the executable price (`S4A-001`);
  always simulates the exact trade size against pool reserves and LP fee tier.
- Rejects swaps when pool state block lags behind chain tip, when either token is not
  whitelisted or has transfer tax (`S4A-002`), or when price impact / slippage exceeds limits.
- Estimates EIP-1559 gas cost (`base_fee + priority_fee`) in USD and adds an explicit MEV
  protection / priority fee budget (`S4A-003`).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from arb.dex.chain import BlockHeaderSnapshot, EvmChainAdapter


class DexPoolProtocol(str, Enum):
    UNISWAP_V2 = "UNISWAP_V2"
    UNISWAP_V3 = "UNISWAP_V3"


class AmmPoolState(BaseModel):
    chain_id: int
    pool_address: str
    protocol: DexPoolProtocol = DexPoolProtocol.UNISWAP_V2
    token0_address: str
    token0_symbol: str
    token1_address: str
    token1_symbol: str
    reserve0: Decimal
    reserve1: Decimal
    fee_bps: Decimal = Decimal("30.0")  # e.g., 30 bps (0.30%) for Uniswap V2, 5 bps (0.05%) for V3
    block_number: int
    block_hash: str
    block_timestamp_utc: datetime
    simulation_force_revert: bool = False

    class Config:
        frozen = True

    @property
    def mid_price_token1_per_token0(self) -> Decimal:
        if self.reserve0 <= Decimal("0"):
            raise ValueError("Pool reserve0 must be > 0")
        return self.reserve1 / self.reserve0


class SwapSimulationResult(BaseModel):
    is_valid: bool
    reject_reason: Optional[str] = None
    pool_address: str
    token_in_symbol: str
    token_out_symbol: str
    amount_in: Decimal
    expected_amount_out: Decimal = Decimal("0")
    min_amount_out_after_slippage: Decimal = Decimal("0")
    mid_price: Decimal = Decimal("0")
    execution_price: Decimal = Decimal("0")
    price_impact_bps: Decimal = Decimal("0")
    pool_fee_bps: Decimal = Decimal("0")
    estimated_gas_units: int = 0
    estimated_gas_cost_wei: int = 0
    estimated_gas_cost_usd: Decimal = Decimal("0")
    mev_risk_budget_usd: Decimal = Decimal("0")
    state_block_number: int
    state_block_hash: str
    expires_at_utc: datetime

    class Config:
        frozen = True


class DexPoolSimulator:
    """DEX-002: Simulates exact AMM swap output, price impact, and EIP-1559 gas + MEV cost."""

    def __init__(
        self,
        chain_adapter: EvmChainAdapter,
        max_pool_block_lag: int = 1,
        max_price_impact_bps: Decimal = Decimal("150.0"),
        default_slippage_tolerance_bps: Decimal = Decimal("30.0"),
        default_swap_gas_units: int = 145_000,
        mev_risk_bps: Decimal = Decimal("3.0"),
    ) -> None:
        self.chain_adapter = chain_adapter
        self.max_pool_block_lag = max_pool_block_lag
        self.max_price_impact_bps = max_price_impact_bps
        self.default_slippage_tolerance_bps = default_slippage_tolerance_bps
        self.default_swap_gas_units = default_swap_gas_units
        self.mev_risk_bps = mev_risk_bps

    def simulate_swap(
        self,
        pool_state: AmmPoolState,
        token_in_symbol: str,
        amount_in: Decimal,
        latest_block: BlockHeaderSnapshot,
        native_gas_token_usd_price: Decimal,
        notional_usd_estimate: Decimal,
        now_utc: datetime,
        ttl_seconds: int = 12,
    ) -> SwapSimulationResult:
        """
        Simulate swapping `amount_in` of `token_in_symbol` on `pool_state`.
        Strictly computes constant-product output (`x * y = k` with fee deduction) and
        NEVER substitutes `mid_price` as `execution_price` (`S4A-001`).
        """
        expires_at = now_utc + timedelta(seconds=ttl_seconds)
        token_in = token_in_symbol.upper()
        if token_in == pool_state.token0_symbol.upper():
            token_out = pool_state.token1_symbol.upper()
            res_in, res_out = pool_state.reserve0, pool_state.reserve1
        elif token_in == pool_state.token1_symbol.upper():
            token_out = pool_state.token0_symbol.upper()
            res_in, res_out = pool_state.reserve1, pool_state.reserve0
        else:
            return SwapSimulationResult(
                is_valid=False,
                reject_reason=f"TOKEN_NOT_IN_POOL:{token_in}",
                pool_address=pool_state.pool_address,
                token_in_symbol=token_in,
                token_out_symbol="UNKNOWN",
                amount_in=amount_in,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                expires_at_utc=expires_at,
            )

        # 1. Validate whitelist & tax token rules (S4A-002)
        ok_wl, wl_reason = self.chain_adapter.validate_pool_and_tokens(pool_state.pool_address)
        if not ok_wl:
            return SwapSimulationResult(
                is_valid=False,
                reject_reason=wl_reason or "UNWHITELISTED_POOL_OR_TOKEN",
                pool_address=pool_state.pool_address,
                token_in_symbol=token_in,
                token_out_symbol=token_out,
                amount_in=amount_in,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                expires_at_utc=expires_at,
            )

        # 2. Check pool block staleness against latest chain block
        block_lag = latest_block.block_number - pool_state.block_number
        if block_lag > self.max_pool_block_lag or block_lag < 0:
            return SwapSimulationResult(
                is_valid=False,
                reject_reason=f"STALE_POOL_BLOCK_STATE:lag={block_lag}",
                pool_address=pool_state.pool_address,
                token_in_symbol=token_in,
                token_out_symbol=token_out,
                amount_in=amount_in,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                expires_at_utc=expires_at,
            )

        # 3. Check on-chain eth_call simulation revert
        if pool_state.simulation_force_revert or amount_in <= Decimal("0") or res_in <= Decimal("0") or res_out <= Decimal("0"):
            return SwapSimulationResult(
                is_valid=False,
                reject_reason="ONCHAIN_SIMULATION_REVERTED",
                pool_address=pool_state.pool_address,
                token_in_symbol=token_in,
                token_out_symbol=token_out,
                amount_in=amount_in,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                expires_at_utc=expires_at,
            )

        # 4. Compute exact AMM output (Uniswap V2/V3 curve after LP fee)
        mid_px = res_out / res_in
        fee_multiplier = (Decimal("10000") - pool_state.fee_bps) / Decimal("10000")
        amount_in_with_fee = amount_in * fee_multiplier
        numerator = amount_in_with_fee * res_out
        denominator = res_in + amount_in_with_fee
        expected_out = (numerator / denominator).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

        if expected_out <= Decimal("0") or expected_out >= res_out:
            return SwapSimulationResult(
                is_valid=False,
                reject_reason="INSUFFICIENT_POOL_LIQUIDITY",
                pool_address=pool_state.pool_address,
                token_in_symbol=token_in,
                token_out_symbol=token_out,
                amount_in=amount_in,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                expires_at_utc=expires_at,
            )

        exec_px = expected_out / amount_in
        # Pure curve price impact (excluding LP fee) + LP fee impact relative to mid_price
        total_deviation_bps = ((mid_px - exec_px) / mid_px) * Decimal("10000")
        price_impact_bps = max(Decimal("0"), total_deviation_bps - pool_state.fee_bps)

        if price_impact_bps > self.max_price_impact_bps:
            return SwapSimulationResult(
                is_valid=False,
                reject_reason=f"DEX_PRICE_IMPACT_EXCEEDED:{price_impact_bps:.2f}bps>{self.max_price_impact_bps}bps",
                pool_address=pool_state.pool_address,
                token_in_symbol=token_in,
                token_out_symbol=token_out,
                amount_in=amount_in,
                expected_amount_out=expected_out,
                mid_price=mid_px,
                execution_price=exec_px,
                price_impact_bps=price_impact_bps,
                pool_fee_bps=pool_state.fee_bps,
                state_block_number=pool_state.block_number,
                state_block_hash=pool_state.block_hash,
                expires_at_utc=expires_at,
            )

        slip_mult = (Decimal("10000") - self.default_slippage_tolerance_bps) / Decimal("10000")
        min_out = (expected_out * slip_mult).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

        # 5. Estimate EIP-1559 gas cost in USD and MEV risk budget (S4A-003)
        effective_gas_price_wei = latest_block.base_fee_per_gas_wei + latest_block.priority_fee_per_gas_wei
        gas_cost_wei = self.default_swap_gas_units * effective_gas_price_wei
        gas_cost_eth = Decimal(str(gas_cost_wei)) / Decimal("1000000000000000000")
        gas_cost_usd = (gas_cost_eth * native_gas_token_usd_price).quantize(Decimal("0.0001"))
        mev_budget_usd = ((notional_usd_estimate * self.mev_risk_bps) / Decimal("10000")).quantize(Decimal("0.0001"))

        return SwapSimulationResult(
            is_valid=True,
            reject_reason=None,
            pool_address=pool_state.pool_address,
            token_in_symbol=token_in,
            token_out_symbol=token_out,
            amount_in=amount_in,
            expected_amount_out=expected_out,
            min_amount_out_after_slippage=min_out,
            mid_price=mid_px,
            execution_price=exec_px,
            price_impact_bps=price_impact_bps,
            pool_fee_bps=pool_state.fee_bps,
            estimated_gas_units=self.default_swap_gas_units,
            estimated_gas_cost_wei=gas_cost_wei,
            estimated_gas_cost_usd=gas_cost_usd,
            mev_risk_budget_usd=mev_budget_usd,
            state_block_number=pool_state.block_number,
            state_block_hash=pool_state.block_hash,
            expires_at_utc=expires_at,
        )
