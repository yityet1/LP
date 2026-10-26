"""[COST-001] Level-by-Level Fill Simulation, Fee Schedule, Rule Quantization & Capacity Cost Engine."""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional

from pydantic import BaseModel

from arb.domain.models import OrderBook, OrderSide, PriceLevel, PriceType
from arb.instruments.registry import (
    InstrumentNotTradableError,
    QuantizedOrderSpec,
    TradingRules,
)


class FeeSchedule(BaseModel):
    venue_id: str
    tier_name: str = "VIP0"
    maker_fee_rate: Decimal = Decimal("0.0010")  # 10 bps default
    taker_fee_rate: Decimal = Decimal("0.0010")  # 10 bps default
    fee_discount_asset: Optional[str] = None     # e.g., "BNB"
    fee_discount_rate: Decimal = Decimal("1.0")  # e.g., 0.75 for 25% BNB discount


class FeeEstimate(BaseModel):
    fee_amount: Decimal
    fee_asset: str
    fee_in_settlement_asset: Decimal
    effective_fee_rate: Decimal


class SimulatedLegFill(BaseModel):
    venue_id: str
    instrument_id: str
    side: OrderSide
    rules_version: str
    requested_base_qty: Decimal
    quantized_base_qty: Decimal
    filled_base_qty: Decimal
    unfilled_base_qty: Decimal
    quote_notional: Decimal
    vwap_fill_price: Decimal
    best_level_price: Decimal
    slippage_bps: Decimal
    price_type: PriceType = PriceType.ESTIMATED_FILL_PRICE
    levels_consumed: int
    available_depth_qty: Decimal
    is_fully_executable: bool
    reject_reason: Optional[str] = None


class NetPnlEstimate(BaseModel):
    settlement_asset: str
    initial_capital: Decimal
    final_capital_before_fees: Decimal
    gross_edge: Decimal
    total_fees_in_settlement: Decimal
    dust_residual_value_in_settlement: Decimal
    slippage_cost_in_settlement: Decimal
    expected_net_pnl: Decimal
    net_edge_bps: Decimal


class CostEngine:
    """
    [COST-001] Executes level-by-level L2 order book fill simulations, trading-rule quantization
    (never Python `round()`), multi-asset fee estimation, and net PnL calculation.
    Strictly forbids using `last_trade_price`, `mark_price`, or `index_price` as fill price.
    """

    def __init__(self, fee_schedules: Optional[Dict[str, FeeSchedule]] = None) -> None:
        self._fees: Dict[str, FeeSchedule] = fee_schedules or {
            "binance_spot": FeeSchedule(
                venue_id="binance_spot",
                tier_name="VIP0",
                maker_fee_rate=Decimal("0.0010"),
                taker_fee_rate=Decimal("0.0010"),
            ),
            "okx_spot": FeeSchedule(
                venue_id="okx_spot",
                tier_name="Lv1",
                maker_fee_rate=Decimal("0.0008"),
                taker_fee_rate=Decimal("0.0010"),
            ),
        }

    def get_fee_schedule(self, venue_id: str) -> FeeSchedule:
        return self._fees.get(venue_id, FeeSchedule(venue_id=venue_id))

    @staticmethod
    def quantize_order(
        rules: TradingRules,
        side: OrderSide,
        raw_qty: Decimal,
        raw_limit_price: Decimal,
    ) -> QuantizedOrderSpec:
        """Delegate strictly to `TradingRules` Decimal quantization (never `round()`)."""
        return rules.validate_and_quantize_order(
            side=side,
            raw_qty=raw_qty,
            raw_price=raw_limit_price,
        )

    def simulate_fill(
        self,
        book: OrderBook,
        rules: TradingRules,
        side: OrderSide,
        target_base_qty: Decimal,
    ) -> SimulatedLegFill:
        """
        Walk L2 order book levels to simulate executing `target_base_qty` of base asset.
        - BUY walks `book.asks` (lowest ask first).
        - SELL walks `book.bids` (highest bid first).
        - Quantizes `target_base_qty` via `rules.step_size` (`ROUND_DOWN`) and checks `min_qty` and `min_notional`.
        """
        if not book.is_valid:
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=side,
                rules_version=rules.rules_version,
                requested_base_qty=target_base_qty,
                quantized_base_qty=Decimal("0"),
                filled_base_qty=Decimal("0"),
                unfilled_base_qty=target_base_qty,
                quote_notional=Decimal("0"),
                vwap_fill_price=Decimal("0.00000001"),
                best_level_price=Decimal("0.00000001"),
                slippage_bps=Decimal("0"),
                levels_consumed=0,
                available_depth_qty=Decimal("0"),
                is_fully_executable=False,
                reject_reason="BOOK_INVALID",
            )

        levels: List[PriceLevel] = book.asks if side == OrderSide.BUY else book.bids
        if not levels:
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=side,
                rules_version=rules.rules_version,
                requested_base_qty=target_base_qty,
                quantized_base_qty=Decimal("0"),
                filled_base_qty=Decimal("0"),
                unfilled_base_qty=target_base_qty,
                quote_notional=Decimal("0"),
                vwap_fill_price=Decimal("0.00000001"),
                best_level_price=Decimal("0.00000001"),
                slippage_bps=Decimal("0"),
                levels_consumed=0,
                available_depth_qty=Decimal("0"),
                is_fully_executable=False,
                reject_reason="INSUFFICIENT_DEPTH_EMPTY_BOOK",
            )

        best_px = levels[0].price
        total_avail_depth = sum((lvl.quantity for lvl in levels), Decimal("0"))

        try:
            q_qty = rules.quantize_quantity(target_base_qty)
        except (InstrumentNotTradableError, ValueError) as exc:
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=side,
                rules_version=rules.rules_version,
                requested_base_qty=target_base_qty,
                quantized_base_qty=Decimal("0"),
                filled_base_qty=Decimal("0"),
                unfilled_base_qty=target_base_qty,
                quote_notional=Decimal("0"),
                vwap_fill_price=best_px,
                best_level_price=best_px,
                slippage_bps=Decimal("0"),
                levels_consumed=0,
                available_depth_qty=total_avail_depth,
                is_fully_executable=False,
                reject_reason=f"STEP_OR_MIN_QTY_REJECT:{exc}",
            )

        remaining = q_qty
        filled = Decimal("0")
        quote_notional = Decimal("0")
        consumed = 0

        for lvl in levels:
            if remaining <= Decimal("0"):
                break
            take = min(remaining, lvl.quantity)
            filled += take
            quote_notional += take * lvl.price * rules.contract_multiplier
            remaining -= take
            consumed += 1

        if remaining > Decimal("0") or filled < q_qty:
            vwap = (quote_notional / (filled * rules.contract_multiplier)) if filled > Decimal("0") else best_px
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=side,
                rules_version=rules.rules_version,
                requested_base_qty=target_base_qty,
                quantized_base_qty=q_qty,
                filled_base_qty=filled,
                unfilled_base_qty=remaining,
                quote_notional=quote_notional,
                vwap_fill_price=vwap,
                best_level_price=best_px,
                slippage_bps=Decimal("0"),
                levels_consumed=consumed,
                available_depth_qty=total_avail_depth,
                is_fully_executable=False,
                reject_reason="INSUFFICIENT_DEPTH",
            )

        vwap = quote_notional / (filled * rules.contract_multiplier)
        if quote_notional < rules.min_notional:
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=side,
                rules_version=rules.rules_version,
                requested_base_qty=target_base_qty,
                quantized_base_qty=q_qty,
                filled_base_qty=filled,
                unfilled_base_qty=Decimal("0"),
                quote_notional=quote_notional,
                vwap_fill_price=vwap,
                best_level_price=best_px,
                slippage_bps=Decimal("0"),
                levels_consumed=consumed,
                available_depth_qty=total_avail_depth,
                is_fully_executable=False,
                reject_reason="MIN_NOTIONAL_NOT_MET",
            )

        if side == OrderSide.BUY:
            slip_bps = ((vwap - best_px) / best_px) * Decimal("10000")
        else:
            slip_bps = ((best_px - vwap) / best_px) * Decimal("10000")

        return SimulatedLegFill(
            venue_id=book.venue_id,
            instrument_id=book.instrument_id,
            side=side,
            rules_version=rules.rules_version,
            requested_base_qty=target_base_qty,
            quantized_base_qty=q_qty,
            filled_base_qty=filled,
            unfilled_base_qty=Decimal("0"),
            quote_notional=quote_notional,
            vwap_fill_price=vwap,
            best_level_price=best_px,
            slippage_bps=max(Decimal("0"), slip_bps),
            levels_consumed=consumed,
            available_depth_qty=total_avail_depth,
            is_fully_executable=True,
            reject_reason=None,
        )

    def simulate_buy_with_quote_budget(
        self,
        book: OrderBook,
        rules: TradingRules,
        quote_budget: Decimal,
    ) -> SimulatedLegFill:
        """
        Determine maximum base quantity purchasable with `quote_budget` walking ASK levels,
        then quantize to `rules.step_size` and run `simulate_fill`.
        """
        if not book.is_valid or not book.asks or quote_budget <= Decimal("0"):
            return self.simulate_fill(book, rules, OrderSide.BUY, Decimal("0.00000001"))

        rem_quote = quote_budget
        raw_base = Decimal("0")
        for lvl in book.asks:
            lvl_cost = lvl.quantity * lvl.price * rules.contract_multiplier
            if rem_quote >= lvl_cost:
                raw_base += lvl.quantity
                rem_quote -= lvl_cost
            else:
                raw_base += rem_quote / (lvl.price * rules.contract_multiplier)
                rem_quote = Decimal("0")
                break

        if rem_quote > Decimal("0"):
            # Even consuming entire ask book cannot spend budget -> depth insufficient
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=OrderSide.BUY,
                rules_version=rules.rules_version,
                requested_base_qty=raw_base,
                quantized_base_qty=raw_base,
                filled_base_qty=raw_base,
                unfilled_base_qty=rem_quote / book.asks[-1].price,
                quote_notional=quote_budget - rem_quote,
                vwap_fill_price=book.asks[0].price,
                best_level_price=book.asks[0].price,
                slippage_bps=Decimal("0"),
                levels_consumed=len(book.asks),
                available_depth_qty=raw_base,
                is_fully_executable=False,
                reject_reason="INSUFFICIENT_DEPTH",
            )

        steps = (raw_base / rules.step_size).to_integral_value(rounding=ROUND_DOWN)
        q_base = steps * rules.step_size
        if q_base <= Decimal("0"):
            return SimulatedLegFill(
                venue_id=book.venue_id,
                instrument_id=book.instrument_id,
                side=OrderSide.BUY,
                rules_version=rules.rules_version,
                requested_base_qty=raw_base,
                quantized_base_qty=Decimal("0"),
                filled_base_qty=Decimal("0"),
                unfilled_base_qty=raw_base,
                quote_notional=Decimal("0"),
                vwap_fill_price=book.asks[0].price,
                best_level_price=book.asks[0].price,
                slippage_bps=Decimal("0"),
                levels_consumed=0,
                available_depth_qty=sum((x.quantity for x in book.asks), Decimal("0")),
                is_fully_executable=False,
                reject_reason="STEP_OR_MIN_QTY_REJECT",
            )
        return self.simulate_fill(book, rules, OrderSide.BUY, q_base)

    def estimate_fees(
        self,
        venue_id: str,
        notional_in_settlement: Decimal,
        settlement_asset: str,
        is_maker: bool = False,
        third_asset_price_in_settlement: Optional[Decimal] = None,
    ) -> FeeEstimate:
        """
        Compute trading fee in settlement asset or third fee-discount asset (e.g. BNB).
        """
        sched = self.get_fee_schedule(venue_id)
        base_rate = sched.maker_fee_rate if is_maker else sched.taker_fee_rate

        if sched.fee_discount_asset and third_asset_price_in_settlement and third_asset_price_in_settlement > Decimal("0"):
            eff_rate = base_rate * sched.fee_discount_rate
            fee_in_settlement = notional_in_settlement * eff_rate
            fee_amt_third = fee_in_settlement / third_asset_price_in_settlement
            return FeeEstimate(
                fee_amount=fee_amt_third,
                fee_asset=sched.fee_discount_asset,
                fee_in_settlement_asset=fee_in_settlement,
                effective_fee_rate=eff_rate,
            )

        fee_in_settlement = notional_in_settlement * base_rate
        return FeeEstimate(
            fee_amount=fee_in_settlement,
            fee_asset=settlement_asset,
            fee_in_settlement_asset=fee_in_settlement,
            effective_fee_rate=base_rate,
        )

    @staticmethod
    def estimate_net_pnl(
        settlement_asset: str,
        initial_capital: Decimal,
        final_capital_before_fees: Decimal,
        total_fees_in_settlement: Decimal,
        dust_residual_value_in_settlement: Decimal = Decimal("0"),
        slippage_cost_in_settlement: Decimal = Decimal("0"),
    ) -> NetPnlEstimate:
        """
        Compute gross edge and net PnL after deducting all known fees.
        Note: Unconverted dust residuals are NOT counted as realized settlement cash in net PnL,
        ensuring conservative execution valuation.
        """
        gross_edge = final_capital_before_fees - initial_capital
        expected_net_pnl = gross_edge - total_fees_in_settlement
        net_edge_bps = (
            (expected_net_pnl / initial_capital) * Decimal("10000")
            if initial_capital > Decimal("0")
            else Decimal("0")
        )
        return NetPnlEstimate(
            settlement_asset=settlement_asset,
            initial_capital=initial_capital,
            final_capital_before_fees=final_capital_before_fees,
            gross_edge=gross_edge,
            total_fees_in_settlement=total_fees_in_settlement,
            dust_residual_value_in_settlement=dust_residual_value_in_settlement,
            slippage_cost_in_settlement=slippage_cost_in_settlement,
            expected_net_pnl=expected_net_pnl,
            net_edge_bps=net_edge_bps,
        )


class ReferenceCostCalculator:
    """
    Independent reference implementation for triangle and cross-exchange arbitrage verification
    (used in unit/contract tests to cross-check `CostEngine` & `TriangleArbitrageScanner` results).
    """

    @staticmethod
    def compute_three_leg_cycle_reference(
        initial_usdt: Decimal,
        leg1_ask_px: Decimal,
        leg1_step: Decimal,
        leg2_ask_px: Decimal,
        leg2_step: Decimal,
        leg3_bid_px: Decimal,
        leg3_step: Decimal,
        taker_fee_rate: Decimal,
    ) -> Dict[str, Decimal]:
        """
        Independent manual formula for Cycle: USDT -> AssetA (BUY @ Ask) -> AssetB (BUY @ Ask) -> USDT (SELL @ Bid).
        Deducts taker fees at each leg and tracks step_size rounding dust.
        """
        # Leg 1: Spend USDT to buy Asset A at leg1_ask_px
        raw_a = initial_usdt / leg1_ask_px
        qty_a = (raw_a / leg1_step).to_integral_value(rounding=ROUND_DOWN) * leg1_step
        spent_usdt = qty_a * leg1_ask_px
        unspent_usdt_1 = initial_usdt - spent_usdt
        fee_1_usdt = spent_usdt * taker_fee_rate
        net_a = qty_a * (Decimal("1") - taker_fee_rate)

        # Leg 2: Use net_a (quote of pair B/A) to buy Asset B at leg2_ask_px
        raw_b = net_a / leg2_ask_px
        qty_b = (raw_b / leg2_step).to_integral_value(rounding=ROUND_DOWN) * leg2_step
        spent_a = qty_b * leg2_ask_px
        dust_a = net_a - spent_a
        fee_2_usdt = (spent_a * leg1_ask_px) * taker_fee_rate
        net_b = qty_b * (Decimal("1") - taker_fee_rate)

        # Leg 3: Sell net_b for USDT at leg3_bid_px
        qty_b_sell = (net_b / leg3_step).to_integral_value(rounding=ROUND_DOWN) * leg3_step
        dust_b = net_b - qty_b_sell
        gross_usdt_out = qty_b_sell * leg3_bid_px
        fee_3_usdt = gross_usdt_out * taker_fee_rate
        net_usdt_out = gross_usdt_out - fee_3_usdt + unspent_usdt_1

        total_fees_usdt = fee_1_usdt + fee_2_usdt + fee_3_usdt
        net_pnl_usdt = net_usdt_out - initial_usdt
        return {
            "qty_a": qty_a,
            "qty_b": qty_b,
            "qty_b_sell": qty_b_sell,
            "dust_a": dust_a,
            "dust_b": dust_b,
            "total_fees_usdt": total_fees_usdt,
            "final_usdt": net_usdt_out,
            "net_pnl_usdt": net_pnl_usdt,
            "net_edge_bps": (net_pnl_usdt / initial_usdt) * Decimal("10000"),
        }
