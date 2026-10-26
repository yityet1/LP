"""[STRAT-002] Route A: Binance Spot Triangle Arbitrage Scanner (Paper-Only, Incremental & Full Graph Scan)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from arb.config.settings import RiskPolicyConfig
from arb.costs.engine import CostEngine
from arb.domain.models import (
    Instrument,
    Opportunity,
    OpportunityDecision,
    OpportunityLeg,
    OpportunityStatus,
    OrderSide,
    PriceType,
    TradingStatus,
    ensure_utc,
)
from arb.instruments.registry import InstrumentNotTradableError, InstrumentRegistry
from arb.marketdata.health import MarketHealthGate
from arb.marketdata.orderbook import OrderBookBuilder
from arb.strategy.lifecycle import (
    OpportunityLifecycleManager,
    OpportunityRejectReason,
)


class TriangleLegSpec(BaseModel):
    instrument_id: str
    side: OrderSide
    from_asset: str
    to_asset: str


class TriangleCycleSpec(BaseModel):
    cycle_path_id: str
    venue_id: str
    settlement_asset: str
    intermediate_asset_1: str
    intermediate_asset_2: str
    legs: Tuple[TriangleLegSpec, TriangleLegSpec, TriangleLegSpec]


class TriangleArbitrageScanner:
    """
    [STRAT-002] Route A: Binance Spot Triangle Arbitrage Scanner.
    - Precomputes all valid 3-leg closed loops starting and ending in `settlement_asset` (e.g. USDT -> BTC -> ETH -> USDT).
    - Builds an incremental index `instrument_id -> affected_cycles` so single-symbol depth updates only re-evaluate affected triangles,
      producing identical results to a full scan.
    - Strictly uses ASK levels for BUY legs and BID levels for SELL legs.
    - Simulates sequential 3-leg execution with step_size quantization, fee deduction, and dust tracking.
    - Candidates with `expected_net_pnl <= 0` or `net_edge_bps < min_net_edge_bps` are marked `REJECTED` and excluded from the executable pool.
    - Hard-locked to `PAPER` mode (`OpportunityStatus.PAPER` / `OpportunityDecision.PAPER`); never submits orders directly.
    """

    STRATEGY_ID = "strat_route_a_triangle_spot"
    STRATEGY_VERSION = "1.0.0"

    def __init__(
        self,
        venue_id: str,
        settlement_asset: str,
        registry: InstrumentRegistry,
        health_gate: MarketHealthGate,
        cost_engine: CostEngine,
        lifecycle: OpportunityLifecycleManager,
        risk_policy: RiskPolicyConfig,
        initial_capital_quote: Decimal = Decimal("1000.00"),
        candidate_ttl_ms: int = 500,
    ) -> None:
        self._venue_id = venue_id
        self._settlement_asset = settlement_asset.upper()
        self._registry = registry
        self._health_gate = health_gate
        self._cost_engine = cost_engine
        self._lifecycle = lifecycle
        self._risk_policy = risk_policy
        self._initial_capital = min(initial_capital_quote, risk_policy.max_single_order_notional)
        self._candidate_ttl_ms = candidate_ttl_ms

        self._cycles: List[TriangleCycleSpec] = []
        self._symbol_to_cycles: Dict[str, List[TriangleCycleSpec]] = {}

    @property
    def cycles(self) -> List[TriangleCycleSpec]:
        return list(self._cycles)

    @property
    def symbol_to_cycles(self) -> Dict[str, List[TriangleCycleSpec]]:
        return dict(self._symbol_to_cycles)

    def build_graph(self, instruments: List[Instrument]) -> List[TriangleCycleSpec]:
        """
        Precompute all valid 3-leg closed loops `S -> A -> B -> S` from available spot instruments on `venue_id`,
        and populate `instrument_id -> affected_paths` index.
        """
        # Map directed currency transitions: (from_asset, to_asset) -> TriangleLegSpec
        edges: Dict[Tuple[str, str], TriangleLegSpec] = {}
        for inst in instruments:
            if inst.venue_id != self._venue_id:
                continue
            # Even if currently HALTED, we can index the graph or skip; here we include registered spot pairs
            # and verify live trading_status dynamically during scanning.
            base = inst.base_asset.upper()
            quote = inst.quote_asset.upper()
            # Buying base with quote: from quote -> to base (uses ASK)
            edges[(quote, base)] = TriangleLegSpec(
                instrument_id=inst.instrument_id,
                side=OrderSide.BUY,
                from_asset=quote,
                to_asset=base,
            )
            # Selling base for quote: from base -> to quote (uses BID)
            edges[(base, quote)] = TriangleLegSpec(
                instrument_id=inst.instrument_id,
                side=OrderSide.SELL,
                from_asset=base,
                to_asset=quote,
            )

        s = self._settlement_asset
        first_hop_assets = sorted({to_a for (from_a, to_a) in edges.keys() if from_a == s and to_a != s})

        cycles: List[TriangleCycleSpec] = []
        by_symbol: Dict[str, List[TriangleCycleSpec]] = {}

        for a in first_hop_assets:
            second_hop_assets = sorted(
                {to_b for (from_a, to_b) in edges.keys() if from_a == a and to_b not in (s, a)}
            )
            for b in second_hop_assets:
                if (b, s) in edges:
                    leg1 = edges[(s, a)]
                    leg2 = edges[(a, b)]
                    leg3 = edges[(b, s)]
                    path_id = f"{self._venue_id}:{s}->{a}->{b}->{s}"
                    spec = TriangleCycleSpec(
                        cycle_path_id=path_id,
                        venue_id=self._venue_id,
                        settlement_asset=s,
                        intermediate_asset_1=a,
                        intermediate_asset_2=b,
                        legs=(leg1, leg2, leg3),
                    )
                    cycles.append(spec)
                    for leg in spec.legs:
                        by_symbol.setdefault(leg.instrument_id, []).append(spec)

        self._cycles = cycles
        self._symbol_to_cycles = by_symbol
        return cycles

    def evaluate_cycle(
        self,
        cycle: TriangleCycleSpec,
        order_books: Dict[str, OrderBookBuilder],
        now_utc: Optional[datetime] = None,
        third_fee_asset_prices_usdt: Optional[Dict[str, Decimal]] = None,
    ) -> Opportunity:
        """
        Evaluate a single 3-leg triangle cycle:
        1. Lifecycle starts at `DETECTED`.
        2. Check `TradingRules` tradability & `MarketHealthGate` across all 3 legs (`VALIDATED`).
        3. Simulate sequential leg execution with `CostEngine` (`COSTED`).
        4. Check `expected_net_pnl > 0` and `net_edge_bps >= min_net_edge_bps` (`RISK_CHECKED -> PAPER` or `REJECTED`).
        """
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        expires_at = ref_now + timedelta(milliseconds=self._candidate_ttl_ms)

        leg_builders: List[OrderBookBuilder] = []
        input_snap_ids: List[str] = []
        reject_reasons: List[str] = []

        # Verify all 3 instruments are tradable in InstrumentRegistry
        leg_rules = []
        for leg_spec in cycle.legs:
            try:
                rules = self._registry.get_rules(
                    self._venue_id,
                    leg_spec.instrument_id,
                    now_utc=ref_now,
                    require_tradable=True,
                )
                leg_rules.append(rules)
            except Exception:
                reject_reasons.append(OpportunityRejectReason.INSTRUMENT_NOT_TRADABLE.value)

            builder = order_books.get(leg_spec.instrument_id)
            if builder is not None:
                leg_builders.append(builder)
                input_snap_ids.append(f"{self._venue_id}:{leg_spec.instrument_id}:{builder.last_update_id}")
            else:
                reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)

        # Market health gate check across all 3 legs
        if len(leg_builders) == 3:
            health_ok, health_reports, health_reasons = self._health_gate.evaluate_multi_book_gate(
                leg_builders, now_utc=ref_now
            )
            if not health_ok:
                reject_reasons.append(OpportunityRejectReason.MARKET_HEALTH_INVALID.value)
                reject_reasons.extend(health_reasons)

        if reject_reasons:
            opp = Opportunity(
                strategy_id=self.STRATEGY_ID,
                strategy_version=self.STRATEGY_VERSION,
                status=OpportunityStatus.DETECTED,
                legs=[],
                input_snapshot_ids=input_snap_ids or ["missing_snapshots"],
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

        # Simulate sequential 3-leg conversions starting with `self._initial_capital` of settlement_asset (e.g. USDT)
        current_asset_amt = self._initial_capital
        unspent_settlement_cash = Decimal("0")
        total_fees_settlement = Decimal("0")
        total_dust_settlement = Decimal("0")
        total_slippage_settlement = Decimal("0")
        opp_legs: List[OpportunityLeg] = []

        # Track intermediate asset valuation in settlement_asset (USDT)
        asset_value_in_settlement: Dict[str, Decimal] = {self._settlement_asset: Decimal("1")}

        for idx, (leg_spec, rules, builder) in enumerate(zip(cycle.legs, leg_rules, leg_builders)):
            book = builder.to_order_book()
            inst = self._registry.get(self._venue_id, leg_spec.instrument_id)

            if leg_spec.side == OrderSide.BUY:
                # We hold `quote_asset` (`current_asset_amt`) and want to BUY `base_asset` using ASK levels
                sim = self._cost_engine.simulate_buy_with_quote_budget(
                    book=book,
                    rules=rules,
                    quote_budget=current_asset_amt,
                )
            else:
                # We hold `base_asset` (`current_asset_amt`) and want to SELL `base_asset` for `quote_asset` using BID levels
                sim = self._cost_engine.simulate_fill(
                    book=book,
                    rules=rules,
                    side=OrderSide.SELL,
                    target_base_qty=current_asset_amt,
                )

            if not sim.is_fully_executable:
                reason = sim.reject_reason or OpportunityRejectReason.INSUFFICIENT_DEPTH.value
                opp = Opportunity(
                    strategy_id=self.STRATEGY_ID,
                    strategy_version=self.STRATEGY_VERSION,
                    status=OpportunityStatus.DETECTED,
                    legs=opp_legs,
                    input_snapshot_ids=input_snap_ids,
                    settlement_asset=self._settlement_asset,
                    gross_edge=Decimal("0"),
                    fees=total_fees_settlement,
                    expected_net_pnl=Decimal("-1"),
                    net_edge_bps=Decimal("-100"),
                    capacity=Decimal("0"),
                    expires_at=expires_at,
                    decision=OpportunityDecision.REJECT,
                    event_time_utc=ref_now,
                    receive_time_utc=ref_now,
                )
                self._lifecycle.transition(
                    opp,
                    OpportunityStatus.REJECTED,
                    now_utc=ref_now,
                    reject_reasons=[reason, f"LEG_{idx+1}_{leg_spec.instrument_id}:{reason}"],
                )
                return self._lifecycle.register_and_persist(opp, now_utc=ref_now)

            # Calculate leg notional in settlement asset
            quote_to_settlement = asset_value_in_settlement.get(inst.quote_asset.upper(), Decimal("1"))
            leg_notional_settlement = sim.quote_notional * quote_to_settlement
            base_val_settlement = sim.vwap_fill_price * quote_to_settlement
            asset_value_in_settlement[inst.base_asset.upper()] = base_val_settlement

            # Fee estimation (supports third asset like BNB if configured)
            sched = self._cost_engine.get_fee_schedule(self._venue_id)
            third_px = (
                (third_fee_asset_prices_usdt or {}).get(sched.fee_discount_asset)
                if sched.fee_discount_asset
                else None
            )
            fee_est = self._cost_engine.estimate_fees(
                venue_id=self._venue_id,
                notional_in_settlement=leg_notional_settlement,
                settlement_asset=self._settlement_asset,
                is_maker=False,
                third_asset_price_in_settlement=third_px,
            )
            total_fees_settlement += fee_est.fee_in_settlement_asset
            total_slippage_settlement += (sim.slippage_bps / Decimal("10000")) * leg_notional_settlement

            # Propagate net output quantity to next leg and record any quantized dust residual
            if leg_spec.side == OrderSide.BUY:
                unspent_quote = max(Decimal("0"), current_asset_amt - sim.quote_notional)
                if idx == 0:
                    # Unspent USDT on leg 1 remains in our USDT wallet!
                    unspent_settlement_cash += unspent_quote
                else:
                    total_dust_settlement += unspent_quote * quote_to_settlement

                if sched.fee_discount_asset and third_px:
                    # Fee paid in BNB -> full base qty proceeds to next leg
                    current_asset_amt = sim.filled_base_qty
                else:
                    current_asset_amt = sim.filled_base_qty * (Decimal("1") - fee_est.effective_fee_rate)
            else:
                unspent_base = max(Decimal("0"), current_asset_amt - sim.filled_base_qty)
                total_dust_settlement += unspent_base * base_val_settlement

                if sched.fee_discount_asset and third_px:
                    current_asset_amt = sim.quote_notional
                else:
                    current_asset_amt = sim.quote_notional * (Decimal("1") - fee_est.effective_fee_rate)

            opp_legs.append(
                OpportunityLeg(
                    venue_id=self._venue_id,
                    instrument_id=leg_spec.instrument_id,
                    side=leg_spec.side,
                    target_quantity=sim.quantized_base_qty,
                    executable_price=sim.vwap_fill_price,
                    executable_price_type=PriceType.ESTIMATED_FILL_PRICE,
                    available_depth_qty=sim.available_depth_qty,
                    expected_fee=fee_est.fee_amount,
                    fee_asset=fee_est.fee_asset,
                    rules_version=rules.rules_version,
                )
            )

        # Final capital in settlement asset (e.g. USDT)
        final_settlement_cash = current_asset_amt + unspent_settlement_cash
        final_before_fees = final_settlement_cash + total_fees_settlement
        pnl_est = self._cost_engine.estimate_net_pnl(
            settlement_asset=self._settlement_asset,
            initial_capital=self._initial_capital,
            final_capital_before_fees=final_before_fees,
            total_fees_in_settlement=total_fees_settlement,
            dust_residual_value_in_settlement=total_dust_settlement,
            slippage_cost_in_settlement=total_slippage_settlement,
        )

        opp = Opportunity(
            strategy_id=self.STRATEGY_ID,
            strategy_version=self.STRATEGY_VERSION,
            status=OpportunityStatus.DETECTED,
            legs=opp_legs,
            input_snapshot_ids=input_snap_ids,
            settlement_asset=self._settlement_asset,
            gross_edge=pnl_est.gross_edge,
            fees=pnl_est.total_fees_in_settlement,
            slippage=pnl_est.slippage_cost_in_settlement,
            expected_net_pnl=pnl_est.expected_net_pnl,
            net_edge_bps=pnl_est.net_edge_bps,
            capacity=self._initial_capital - unspent_settlement_cash,
            expires_at=expires_at,
            decision=OpportunityDecision.PAPER,
            event_time_utc=ref_now,
            receive_time_utc=ref_now,
        )

        # Advance through lifecycle states: DETECTED -> VALIDATED -> COSTED
        self._lifecycle.transition(opp, OpportunityStatus.VALIDATED, now_utc=ref_now)
        self._lifecycle.transition(opp, OpportunityStatus.COSTED, now_utc=ref_now)

        # Gate 1: Net PnL must be strictly positive
        if opp.expected_net_pnl <= Decimal("0"):
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.NON_POSITIVE_NET_PNL.value],
            )
            return self._lifecycle.register_and_persist(opp, now_utc=ref_now)

        # Gate 2: Net edge bps must meet or exceed `min_net_edge_bps` (e.g. 5.0 bps)
        if opp.net_edge_bps < self._risk_policy.min_net_edge_bps:
            self._lifecycle.transition(
                opp,
                OpportunityStatus.REJECTED,
                now_utc=ref_now,
                reject_reasons=[OpportunityRejectReason.BELOW_MIN_NET_EDGE_BPS.value],
            )
            return self._lifecycle.register_and_persist(opp, now_utc=ref_now)

        # Passes all checks -> transition to PAPER (strictly paper-only in Sprint 3)
        self._lifecycle.transition(opp, OpportunityStatus.RISK_CHECKED, now_utc=ref_now)
        self._lifecycle.transition(opp, OpportunityStatus.PAPER, now_utc=ref_now)
        return self._lifecycle.register_and_persist(opp, now_utc=ref_now)

    def scan_all(
        self,
        order_books: Dict[str, OrderBookBuilder],
        now_utc: Optional[datetime] = None,
        third_fee_asset_prices_usdt: Optional[Dict[str, Decimal]] = None,
    ) -> Tuple[List[Opportunity], List[Opportunity]]:
        """
        Full graph scan across all precomputed triangle cycles.
        Returns `(executable_paper_pool, all_evaluated_opportunities)`.
        """
        all_opps: List[Opportunity] = []
        executable_pool: List[Opportunity] = []
        for cyc in self._cycles:
            opp = self.evaluate_cycle(
                cyc,
                order_books=order_books,
                now_utc=now_utc,
                third_fee_asset_prices_usdt=third_fee_asset_prices_usdt,
            )
            all_opps.append(opp)
            if opp.status == OpportunityStatus.PAPER and opp.decision == OpportunityDecision.PAPER:
                executable_pool.append(opp)
        return executable_pool, all_opps

    def scan_incremental(
        self,
        updated_instrument_id: str,
        order_books: Dict[str, OrderBookBuilder],
        now_utc: Optional[datetime] = None,
        third_fee_asset_prices_usdt: Optional[Dict[str, Decimal]] = None,
    ) -> Tuple[List[Opportunity], List[Opportunity]]:
        """
        Incremental scan triggered when `updated_instrument_id` receives an L2 book update.
        Evaluates only affected triangle paths in `self._symbol_to_cycles[updated_instrument_id]`.
        """
        affected = self._symbol_to_cycles.get(updated_instrument_id, [])
        all_opps: List[Opportunity] = []
        executable_pool: List[Opportunity] = []
        for cyc in affected:
            opp = self.evaluate_cycle(
                cyc,
                order_books=order_books,
                now_utc=now_utc,
                third_fee_asset_prices_usdt=third_fee_asset_prices_usdt,
            )
            all_opps.append(opp)
            if opp.status == OpportunityStatus.PAPER and opp.decision == OpportunityDecision.PAPER:
                executable_pool.append(opp)
        return executable_pool, all_opps
