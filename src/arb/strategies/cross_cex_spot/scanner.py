"""[STRAT-003] Cross-Exchange Spot Arbitrage Scanner & Pre-Positioned Inventory Executability Engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from arb.config.settings import RiskPolicyConfig
from arb.costs.engine import CostEngine
from arb.domain.models import (
    BalanceSnapshot,
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


class FxRateQuote(BaseModel):
    """Explicit FX conversion rate and cost when two venues quote in different currencies (S1-009)."""
    from_asset: str
    to_asset: str
    rate: Decimal
    conversion_cost_bps: Decimal = Decimal("1.0")
    is_reliable: bool = True
    updated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VenueTransferStatus(BaseModel):
    """Tracks deposit/withdrawal status for out-of-band inventory rebalancing (S1-007)."""
    venue_id: str
    asset: str
    deposit_enabled: bool = True
    withdrawal_enabled: bool = True


class CrossCexPairMapping(BaseModel):
    canonical_pair: str
    venue_a_id: str = "binance_spot"
    venue_a_instrument_id: str
    venue_b_id: str = "okx_spot"
    venue_b_instrument_id: str
    target_base_qty: Decimal


class CrossCexInventoryState(BaseModel):
    """
    [STRAT-003 / S1-007 / S1-008] Tracks pre-positioned inventory across venues,
    computes post-trade inventory skew, and flags rebalancing alerts without blocking
    pre-positioned spot execution when deposits/withdrawals are paused.
    """
    buy_venue_id: str
    sell_venue_id: str
    base_asset: str
    quote_asset: str
    buy_venue_quote_allocatable: Decimal
    sell_venue_base_allocatable: Decimal
    post_trade_buy_venue_base_share: Decimal
    rebalance_alert_triggered: bool = False
    rebalance_blocked_by_transfer_pause: bool = False
    inventory_notes: List[str] = Field(default_factory=list)


class CrossCexScanReport(BaseModel):
    opportunity: Opportunity
    buy_venue_id: str
    sell_venue_id: str
    inventory_state: Optional[CrossCexInventoryState] = None


class CrossCexSpotScanner:
    """
    [STRAT-003] S1 Cross-Exchange Spot Arbitrage Scanner (`binance_spot` <-> `okx_spot`).
    Enforces all S1 P0/P1 acceptance gates (`S1-001` through `S1-012`):
    - `S1-001`: Uses low-price venue ASK to buy and high-price venue BID to sell.
    - `S1-002`: Applies each venue's respective `FeeSchedule`.
    - `S1-003`: Applies each venue's respective `TradingRules` (`tick_size`, `step_size`, `min_notional`, `min_qty`, `trading_status`).
    - `S1-005`: Enforces pre-positioned inventory on both sides (never assumes instant cross-exchange transfer).
      Scales down order quantity if constrained by available balance, or rejects with `INSUFFICIENT_INVENTORY` if below `min_notional`/`min_qty`.
    - `S1-006`: Rejects candidate if either venue's market data is stale or cross-venue time skew exceeds `max_cross_venue_skew_ms`.
    - `S1-007`: Deposit/withdrawal suspension does NOT block pre-positioned inventory trading, but marks rebalancing plan as blocked.
    - `S1-008`: Triggers inventory skew / rebalancing alert when post-trade base asset concentration on one venue exceeds `max_venue_inventory_skew_ratio`.
    - `S1-009`: When two venues quote in different quote assets (e.g. USDT vs USDC), explicitly applies `FxRateQuote` rate and conversion cost;
      rejects with `UNRELIABLE_FX_CONVERSION` if FX rate is missing or unreliable.
    """

    STRATEGY_ID = "strat_s1_cross_cex_spot"
    STRATEGY_VERSION = "1.0.0"

    def __init__(
        self,
        registry: InstrumentRegistry,
        health_gate: MarketHealthGate,
        cost_engine: CostEngine,
        lifecycle: OpportunityLifecycleManager,
        risk_policy: RiskPolicyConfig,
        settlement_asset: str = "USDT",
        max_venue_inventory_skew_ratio: Decimal = Decimal("0.80"),
        candidate_ttl_ms: int = 500,
    ) -> None:
        self._registry = registry
        self._health_gate = health_gate
        self._cost_engine = cost_engine
        self._lifecycle = lifecycle
        self._risk_policy = risk_policy
        self._settlement_asset = settlement_asset.upper()
        self._max_skew_ratio = max_venue_inventory_skew_ratio
        self._candidate_ttl_ms = candidate_ttl_ms

    @staticmethod
    def _lookup_allocatable(
        balances: Dict[Tuple[str, str], BalanceSnapshot],
        venue_id: str,
        asset: str,
    ) -> Decimal:
        snap = balances.get((venue_id, asset.upper()))
        if snap is None:
            return Decimal("0")
        return snap.allocatable_balance

    def evaluate_direction(
        self,
        canonical_pair: str,
        buy_venue_id: str,
        buy_instrument_id: str,
        sell_venue_id: str,
        sell_instrument_id: str,
        target_base_qty: Decimal,
        order_books: Dict[Tuple[str, str], OrderBookBuilder],
        balances: Dict[Tuple[str, str], BalanceSnapshot],
        fx_rates: Optional[Dict[Tuple[str, str], FxRateQuote]] = None,
        transfer_statuses: Optional[Dict[Tuple[str, str], VenueTransferStatus]] = None,
        now_utc: Optional[datetime] = None,
    ) -> CrossCexScanReport:
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        expires_at = ref_now + timedelta(milliseconds=self._candidate_ttl_ms)

        buy_builder = order_books.get((buy_venue_id, buy_instrument_id))
        sell_builder = order_books.get((sell_venue_id, sell_instrument_id))
        snap_ids: List[str] = []
        if buy_builder:
            snap_ids.append(f"{buy_venue_id}:{buy_instrument_id}:{buy_builder.last_update_id}")
        if sell_builder:
            snap_ids.append(f"{sell_venue_id}:{sell_instrument_id}:{sell_builder.last_update_id}")

        reject_reasons: List[str] = []

        # 1. Check TradingRules on both venues (S1-003)
        try:
            buy_rules = self._registry.get_rules(buy_venue_id, buy_instrument_id, now_utc=ref_now, require_tradable=True)
            sell_rules = self._registry.get_rules(sell_venue_id, sell_instrument_id, now_utc=ref_now, require_tradable=True)
        except Exception:
            reject_reasons.append(OpportunityRejectReason.INSTRUMENT_NOT_TRADABLE.value)
            buy_rules = None  # type: ignore[assignment]
            sell_rules = None  # type: ignore[assignment]

        # 2. Check MarketHealthGate across both venues (S1-006)
        if buy_builder is None or sell_builder is None:
            reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
        else:
            health_ok, _, health_reasons = self._health_gate.evaluate_multi_book_gate(
                [buy_builder, sell_builder], now_utc=ref_now
            )
            if not health_ok:
                reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
                reject_reasons.extend(health_reasons)

        if reject_reasons or buy_rules is None or sell_rules is None or buy_builder is None or sell_builder is None:
            opp = self._build_rejected_opportunity(
                snap_ids=snap_ids,
                expires_at=expires_at,
                ref_now=ref_now,
                reject_reasons=reject_reasons,
            )
            return CrossCexScanReport(
                opportunity=opp,
                buy_venue_id=buy_venue_id,
                sell_venue_id=sell_venue_id,
            )

        # 3. Check quote currency compatibility / FX conversion (S1-009)
        buy_quote = buy_rules.quote_asset.upper()
        sell_quote = sell_rules.quote_asset.upper()
        fx_rate_buy_to_settlement = Decimal("1")
        fx_rate_sell_to_settlement = Decimal("1")
        fx_cost_bps = Decimal("0")

        if buy_quote != self._settlement_asset:
            fx_q = (fx_rates or {}).get((buy_quote, self._settlement_asset))
            if fx_q is None or not fx_q.is_reliable or fx_q.rate <= Decimal("0"):
                opp = self._build_rejected_opportunity(
                    snap_ids=snap_ids,
                    expires_at=expires_at,
                    ref_now=ref_now,
                    reject_reasons=["UNRELIABLE_FX_CONVERSION", f"MISSING_FX:{buy_quote}->{self._settlement_asset}"],
                )
                return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)
            fx_rate_buy_to_settlement = fx_q.rate
            fx_cost_bps += fx_q.conversion_cost_bps

        if sell_quote != self._settlement_asset:
            fx_q = (fx_rates or {}).get((sell_quote, self._settlement_asset))
            if fx_q is None or not fx_q.is_reliable or fx_q.rate <= Decimal("0"):
                opp = self._build_rejected_opportunity(
                    snap_ids=snap_ids,
                    expires_at=expires_at,
                    ref_now=ref_now,
                    reject_reasons=["UNRELIABLE_FX_CONVERSION", f"MISSING_FX:{sell_quote}->{self._settlement_asset}"],
                )
                return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)
            fx_rate_sell_to_settlement = fx_q.rate
            fx_cost_bps += fx_q.conversion_cost_bps

        # 4. Pre-positioned Inventory Executability Check & Capacity Scaling (S1-005)
        base_asset = buy_rules.base_asset.upper()
        buy_quote_avail = self._lookup_allocatable(balances, buy_venue_id, buy_quote)
        sell_base_avail = self._lookup_allocatable(balances, sell_venue_id, base_asset)
        buy_base_existing = self._lookup_allocatable(balances, buy_venue_id, base_asset)

        buy_book = buy_builder.to_order_book()
        sell_book = sell_builder.to_order_book()

        if not buy_book.asks or not sell_book.bids:
            opp = self._build_rejected_opportunity(
                snap_ids=snap_ids,
                expires_at=expires_at,
                ref_now=ref_now,
                reject_reasons=[OpportunityRejectReason.INSUFFICIENT_DEPTH.value],
            )
            return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)

        best_ask_px = buy_book.asks[0].price
        # Maximum base quantity affordable on buy venue (leaving 0.2% buffer for multi-level slippage)
        max_buy_base_by_quote = (buy_quote_avail * Decimal("0.998")) / best_ask_px if best_ask_px > Decimal("0") else Decimal("0")
        # Maximum base quantity allowed by single-order notional limit
        max_base_by_risk_limit = (
            self._risk_policy.max_single_order_notional / (best_ask_px * fx_rate_buy_to_settlement)
        )

        # Constrain quantity by target, pre-positioned quote balance, pre-positioned base balance, and risk limit
        constrained_raw_qty = min(
            target_base_qty,
            max_buy_base_by_quote,
            sell_base_avail,
            max_base_by_risk_limit,
        )

        if constrained_raw_qty <= Decimal("0"):
            opp = self._build_rejected_opportunity(
                snap_ids=snap_ids,
                expires_at=expires_at,
                ref_now=ref_now,
                reject_reasons=["INSUFFICIENT_PREPOSITIONED_INVENTORY"],
            )
            return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)

        # Quantize to the coarser step_size of the two venues so both legs execute the exact same base quantity!
        common_step = max(buy_rules.step_size, sell_rules.step_size)
        common_steps = (constrained_raw_qty / common_step).to_integral_value(rounding=ROUND_DOWN)
        executable_qty = common_steps * common_step

        if executable_qty < max(buy_rules.min_qty, sell_rules.min_qty) or executable_qty <= Decimal("0"):
            opp = self._build_rejected_opportunity(
                snap_ids=snap_ids,
                expires_at=expires_at,
                ref_now=ref_now,
                reject_reasons=[
                    "INSUFFICIENT_PREPOSITIONED_INVENTORY",
                    OpportunityRejectReason.STEP_OR_MIN_QTY_REJECT.value,
                ],
            )
            return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)

        # 5. Simulate Level-by-Level Fills on Buy Venue (ASK) and Sell Venue (BID) (S1-001, COST-001)
        buy_sim = self._cost_engine.simulate_fill(buy_book, buy_rules, OrderSide.BUY, executable_qty)
        sell_sim = self._cost_engine.simulate_fill(sell_book, sell_rules, OrderSide.SELL, executable_qty)

        if not buy_sim.is_fully_executable or not sell_sim.is_fully_executable:
            reasons = []
            if not buy_sim.is_fully_executable and buy_sim.reject_reason:
                reasons.append(f"BUY_{buy_venue_id}:{buy_sim.reject_reason}")
                reasons.append(buy_sim.reject_reason)
            if not sell_sim.is_fully_executable and sell_sim.reject_reason:
                reasons.append(f"SELL_{sell_venue_id}:{sell_sim.reject_reason}")
                reasons.append(sell_sim.reject_reason)
            opp = self._build_rejected_opportunity(
                snap_ids=snap_ids,
                expires_at=expires_at,
                ref_now=ref_now,
                reject_reasons=reasons or [OpportunityRejectReason.INSUFFICIENT_DEPTH.value],
            )
            return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)

        # Verify actual simulated buy quote notional does not exceed buy_quote_avail
        if buy_sim.quote_notional > buy_quote_avail:
            opp = self._build_rejected_opportunity(
                snap_ids=snap_ids,
                expires_at=expires_at,
                ref_now=ref_now,
                reject_reasons=["INSUFFICIENT_PREPOSITIONED_INVENTORY"],
            )
            return CrossCexScanReport(opportunity=opp, buy_venue_id=buy_venue_id, sell_venue_id=sell_venue_id)

        # 6. Compute Fees, FX Conversion Costs, and Net PnL in Settlement Asset (S1-002, S1-009)
        buy_notional_settlement = buy_sim.quote_notional * fx_rate_buy_to_settlement
        sell_notional_settlement = sell_sim.quote_notional * fx_rate_sell_to_settlement

        buy_fee = self._cost_engine.estimate_fees(
            venue_id=buy_venue_id,
            notional_in_settlement=buy_notional_settlement,
            settlement_asset=self._settlement_asset,
            is_maker=False,
        )
        sell_fee = self._cost_engine.estimate_fees(
            venue_id=sell_venue_id,
            notional_in_settlement=sell_notional_settlement,
            settlement_asset=self._settlement_asset,
            is_maker=False,
        )
        fx_conversion_cost = ((buy_notional_settlement + sell_notional_settlement) / Decimal("2")) * (
            fx_cost_bps / Decimal("10000")
        )
        total_fees = buy_fee.fee_in_settlement_asset + sell_fee.fee_in_settlement_asset + fx_conversion_cost

        pnl_est = self._cost_engine.estimate_net_pnl(
            settlement_asset=self._settlement_asset,
            initial_capital=buy_notional_settlement,
            final_capital_before_fees=sell_notional_settlement,
            total_fees_in_settlement=total_fees,
        )

        # 7. Evaluate Post-Trade Inventory Skew & Transfer/Rebalance Status (S1-007, S1-008)
        total_base_across_venues = buy_base_existing + sell_base_avail
        post_buy_base = buy_base_existing + executable_qty
        post_buy_share = (
            post_buy_base / total_base_across_venues
            if total_base_across_venues > Decimal("0")
            else Decimal("0.5")
        )
        skew_alert = post_buy_share >= self._max_skew_ratio

        # Check if withdrawal on buy_venue or deposit on sell_venue is paused
        buy_tx_status = (transfer_statuses or {}).get((buy_venue_id, base_asset))
        sell_tx_status = (transfer_statuses or {}).get((sell_venue_id, base_asset))
        rebalance_blocked = False
        notes: List[str] = []
        if buy_tx_status and not buy_tx_status.withdrawal_enabled:
            rebalance_blocked = True
            notes.append(f"REBALANCE_BLOCKED:{buy_venue_id}_{base_asset}_WITHDRAWAL_PAUSED")
        if sell_tx_status and not sell_tx_status.deposit_enabled:
            rebalance_blocked = True
            notes.append(f"REBALANCE_BLOCKED:{sell_venue_id}_{base_asset}_DEPOSIT_PAUSED")
        if skew_alert:
            notes.append(
                f"INVENTORY_SKEW_ALERT:{buy_venue_id}_{base_asset}_SHARE_{post_buy_share:.2%}_EXCEEDS_{self._max_skew_ratio:.2%}"
            )

        inv_state = CrossCexInventoryState(
            buy_venue_id=buy_venue_id,
            sell_venue_id=sell_venue_id,
            base_asset=base_asset,
            quote_asset=self._settlement_asset,
            buy_venue_quote_allocatable=buy_quote_avail,
            sell_venue_base_allocatable=sell_base_avail,
            post_trade_buy_venue_base_share=post_buy_share,
            rebalance_alert_triggered=skew_alert,
            rebalance_blocked_by_transfer_pause=rebalance_blocked,
            inventory_notes=notes,
        )

        opp_legs = [
            OpportunityLeg(
                venue_id=buy_venue_id,
                instrument_id=buy_instrument_id,
                side=OrderSide.BUY,
                target_quantity=executable_qty,
                executable_price=buy_sim.vwap_fill_price,
                executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                available_depth_qty=buy_sim.available_depth_qty,
                expected_fee=buy_fee.fee_amount,
                fee_asset=buy_fee.fee_asset,
                rules_version=buy_rules.rules_version,
            ),
            OpportunityLeg(
                venue_id=sell_venue_id,
                instrument_id=sell_instrument_id,
                side=OrderSide.SELL,
                target_quantity=executable_qty,
                executable_price=sell_sim.vwap_fill_price,
                executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                available_depth_qty=sell_sim.available_depth_qty,
                expected_fee=sell_fee.fee_amount,
                fee_asset=sell_fee.fee_asset,
                rules_version=sell_rules.rules_version,
            ),
        ]

        opp = Opportunity(
            strategy_id=self.STRATEGY_ID,
            strategy_version=self.STRATEGY_VERSION,
            status=OpportunityStatus.DETECTED,
            legs=opp_legs,
            input_snapshot_ids=snap_ids,
            settlement_asset=self._settlement_asset,
            gross_edge=pnl_est.gross_edge,
            fees=pnl_est.total_fees_in_settlement,
            expected_net_pnl=pnl_est.expected_net_pnl,
            net_edge_bps=pnl_est.net_edge_bps,
            capacity=buy_notional_settlement,
            expires_at=expires_at,
            decision=OpportunityDecision.PAPER,
            event_time_utc=ref_now,
            receive_time_utc=ref_now,
        )

        self._lifecycle.transition(opp, OpportunityStatus.VALIDATED, now_utc=ref_now)
        self._lifecycle.transition(opp, OpportunityStatus.COSTED, now_utc=ref_now)

        if opp.expected_net_pnl <= Decimal("0"):
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.NON_POSITIVE_NET_PNL.value],
            )
            return CrossCexScanReport(
                opportunity=self._lifecycle.register_and_persist(opp, now_utc=ref_now),
                buy_venue_id=buy_venue_id,
                sell_venue_id=sell_venue_id,
                inventory_state=inv_state,
            )

        if opp.net_edge_bps < self._risk_policy.min_net_edge_bps:
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.BELOW_MIN_NET_EDGE_BPS.value],
            )
            return CrossCexScanReport(
                opportunity=self._lifecycle.register_and_persist(opp, now_utc=ref_now),
                buy_venue_id=buy_venue_id,
                sell_venue_id=sell_venue_id,
                inventory_state=inv_state,
            )

        self._lifecycle.transition(opp, OpportunityStatus.RISK_CHECKED, now_utc=ref_now)
        self._lifecycle.transition(opp, OpportunityStatus.PAPER, now_utc=ref_now)
        return CrossCexScanReport(
            opportunity=self._lifecycle.register_and_persist(opp, now_utc=ref_now),
            buy_venue_id=buy_venue_id,
            sell_venue_id=sell_venue_id,
            inventory_state=inv_state,
        )

    def scan_pair(
        self,
        mapping: CrossCexPairMapping,
        order_books: Dict[Tuple[str, str], OrderBookBuilder],
        balances: Dict[Tuple[str, str], BalanceSnapshot],
        fx_rates: Optional[Dict[Tuple[str, str], FxRateQuote]] = None,
        transfer_statuses: Optional[Dict[Tuple[str, str], VenueTransferStatus]] = None,
        now_utc: Optional[datetime] = None,
    ) -> Tuple[List[CrossCexScanReport], List[CrossCexScanReport]]:
        """
        Scan both directions (`Venue A -> Venue B` and `Venue B -> Venue A`) for a mapped pair,
        and return `(executable_reports_sorted_by_net_pnl, all_reports)`.
        """
        rep_a_to_b = self.evaluate_direction(
            canonical_pair=mapping.canonical_pair,
            buy_venue_id=mapping.venue_a_id,
            buy_instrument_id=mapping.venue_a_instrument_id,
            sell_venue_id=mapping.venue_b_id,
            sell_instrument_id=mapping.venue_b_instrument_id,
            target_base_qty=mapping.target_base_qty,
            order_books=order_books,
            balances=balances,
            fx_rates=fx_rates,
            transfer_statuses=transfer_statuses,
            now_utc=now_utc,
        )
        rep_b_to_a = self.evaluate_direction(
            canonical_pair=mapping.canonical_pair,
            buy_venue_id=mapping.venue_b_id,
            buy_instrument_id=mapping.venue_b_instrument_id,
            sell_venue_id=mapping.venue_a_id,
            sell_instrument_id=mapping.venue_a_instrument_id,
            target_base_qty=mapping.target_base_qty,
            order_books=order_books,
            balances=balances,
            fx_rates=fx_rates,
            transfer_statuses=transfer_statuses,
            now_utc=now_utc,
        )
        all_reps = [rep_a_to_b, rep_b_to_a]
        exec_reps = [
            r
            for r in all_reps
            if r.opportunity.status == OpportunityStatus.PAPER
            and r.opportunity.decision == OpportunityDecision.PAPER
        ]
        exec_reps.sort(key=lambda r: r.opportunity.expected_net_pnl, reverse=True)
        return exec_reps, all_reps

    def _build_rejected_opportunity(
        self,
        snap_ids: List[str],
        expires_at: datetime,
        ref_now: datetime,
        reject_reasons: List[str],
    ) -> Opportunity:
        opp = Opportunity(
            strategy_id=self.STRATEGY_ID,
            strategy_version=self.STRATEGY_VERSION,
            status=OpportunityStatus.DETECTED,
            legs=[],
            input_snapshot_ids=snap_ids or ["missing_snapshots"],
            settlement_asset=self._settlement_asset,
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
        self._lifecycle.transition(opp, OpportunityStatus.REJECTED, now_utc=ref_now, reject_reasons=reject_reasons)
        return self._lifecycle.register_and_persist(opp, now_utc=ref_now)
