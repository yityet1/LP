"""[RISK-001] Unified Portfolio & Strategy Risk Gateway, Concurrency-Safe Fund Reservations & Kill Switch."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.config.settings import ExecutionMode, RiskPolicyConfig, Settings
from arb.domain.models import BalanceSnapshot, ExecutionPlan, OrderSide, ensure_utc
from arb.ledger.store import LedgerStore, RiskDecisionRecord
from arb.marketdata.health import MarketHealth
from arb.observability.telemetry import (
    AlertManager,
    MetricsRegistry,
    StructuredLogger,
    TraceContext,
)


class KillSwitchScope(str, Enum):
    STRATEGY = "STRATEGY"
    VENUE = "VENUE"
    PORTFOLIO = "GLOBAL"


class RiskRejectCode(str, Enum):
    PASS_ALL_GATES = "PASS_ALL_GATES"
    RISK_SERVICE_UNAVAILABLE = "RISK_SERVICE_UNAVAILABLE"
    LIVE_TRADING_DISABLED_GATE = "LIVE_TRADING_DISABLED_GATE"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    MARKET_HEALTH_INVALID = "MARKET_HEALTH_INVALID"
    UNKNOWN_ORDER_THRESHOLD_EXCEEDED = "UNKNOWN_ORDER_THRESHOLD_EXCEEDED"
    SINGLE_ORDER_NOTIONAL_EXCEEDED = "SINGLE_ORDER_NOTIONAL_EXCEEDED"
    CYCLE_NOTIONAL_EXCEEDED = "CYCLE_NOTIONAL_EXCEEDED"
    STRATEGY_NOTIONAL_EXCEEDED = "STRATEGY_NOTIONAL_EXCEEDED"
    VENUE_NOTIONAL_EXCEEDED = "VENUE_NOTIONAL_EXCEEDED"
    PORTFOLIO_NOTIONAL_EXCEEDED = "PORTFOLIO_NOTIONAL_EXCEEDED"
    DAILY_LOSS_LIMIT_EXCEEDED = "DAILY_LOSS_LIMIT_EXCEEDED"
    UNHEDGED_EXPOSURE_EXCEEDED = "UNHEDGED_EXPOSURE_EXCEEDED"
    STALE_BALANCE_SNAPSHOT = "STALE_BALANCE_SNAPSHOT"
    INSUFFICIENT_FREE_BALANCE_FOR_RESERVATION = "INSUFFICIENT_FREE_BALANCE_FOR_RESERVATION"


class AccountRiskState(BaseModel):
    balances: Dict[Tuple[str, str], BalanceSnapshot] = Field(default_factory=dict)
    strategy_active_notional_usdt: Dict[str, Decimal] = Field(default_factory=dict)
    venue_active_notional_usdt: Dict[str, Decimal] = Field(default_factory=dict)
    portfolio_active_notional_usdt: Decimal = Decimal("0")
    strategy_daily_realized_loss_usdt: Dict[str, Decimal] = Field(default_factory=dict)
    portfolio_daily_realized_loss_usdt: Decimal = Decimal("0")
    strategy_unhedged_notional_usdt: Dict[str, Decimal] = Field(default_factory=dict)
    strategy_unknown_order_count: Dict[str, int] = Field(default_factory=dict)
    max_balance_age_seconds: int = 30


class FundReservation(BaseModel):
    reservation_id: str = Field(default_factory=lambda: f"res_{uuid4().hex[:12]}")
    cycle_id: str
    strategy_id: str
    venue_id: str
    asset: str
    reserved_amount: Decimal
    status: str = "ACTIVE"
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RiskDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"rd_{uuid4().hex[:12]}")
    cycle_id: str
    opportunity_id: str
    strategy_id: str
    policy_version: str
    approved: bool
    reason_codes: List[str] = Field(default_factory=list)
    evaluated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_ledger_record(self) -> RiskDecisionRecord:
        return RiskDecisionRecord(
            decision_id=self.decision_id,
            cycle_id=self.cycle_id,
            opportunity_id=self.opportunity_id,
            strategy_id=self.strategy_id,
            policy_version=self.policy_version,
            approved=self.approved,
            reason_codes=list(self.reason_codes),
            evaluated_at_utc=self.evaluated_at_utc,
        )


class RiskGateway:
    """
    [RISK-001] Pre-trade Risk Gateway, Atomic Fund Reservation Manager, and Persistent Kill Switch.
    - Fail-closed if ledger database or risk state is unavailable (`CORE-008`).
    - Prevents double-spending of the same balance across concurrent strategies (`S1-011`, `PORT-001`).
    - Persists strategy/venue/portfolio kill switches in `LedgerStore` so restarts never auto-resume (`CORE-009`).
    """

    def __init__(
        self,
        settings: Settings,
        ledger: LedgerStore,
        metrics: Optional[MetricsRegistry] = None,
        logger: Optional[StructuredLogger] = None,
        alerts: Optional[AlertManager] = None,
    ) -> None:
        self._settings = settings
        self._policy: RiskPolicyConfig = settings.risk_policy
        self._ledger = ledger
        self._metrics = metrics or MetricsRegistry()
        self._logger = logger or StructuredLogger()
        self._alerts = alerts
        self._lock = threading.RLock()
        self._active_reservations: Dict[Tuple[str, str], Decimal] = {}
        self._reservations_by_id: Dict[str, FundReservation] = {}

    def activate_kill_switch(
        self,
        scope: KillSwitchScope,
        target_id: str,
        reason: str,
        triggered_by: str = "RiskGateway",
    ) -> str:
        """
        Activate persistent kill switch at STRATEGY (`STRATEGY:<strategy_id>`),
        VENUE (`VENUE:<venue_id>`), or PORTFOLIO (`GLOBAL`) scope.
        """
        scope_key = "GLOBAL" if scope == KillSwitchScope.PORTFOLIO else f"{scope.value}:{target_id}"
        with self._lock:
            self._ledger.set_kill_switch(scope_key, True, reason, triggered_by)
        if self._alerts:
            self._alerts.trigger(
                "UNKNOWN_ORDER_DETECTED" if "UNKNOWN" in reason.upper() else "LEDGER_RECONCILIATION_MISMATCH",
                f"Kill switch activated [{scope_key}]: {reason}",
                TraceContext(strategy_id=target_id, venue_id=target_id),
            )
        return scope_key

    def is_halted(self, strategy_id: str, venue_ids: List[str]) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        if self._ledger.is_kill_switch_active("GLOBAL"):
            reasons.append("KILL_SWITCH_ACTIVE:GLOBAL")
        if self._ledger.is_kill_switch_active(f"STRATEGY:{strategy_id}"):
            reasons.append(f"KILL_SWITCH_ACTIVE:STRATEGY:{strategy_id}")
        for vid in venue_ids:
            if self._ledger.is_kill_switch_active(f"VENUE:{vid}"):
                reasons.append(f"KILL_SWITCH_ACTIVE:VENUE:{vid}")
        return (len(reasons) > 0), reasons

    def evaluate(
        self,
        plan: ExecutionPlan,
        account_state: Optional[AccountRiskState],
        market_health_list: List[MarketHealth],
        now_utc: Optional[datetime] = None,
        reduce_only: bool = False,
    ) -> RiskDecision:
        """
        Evaluate an `ExecutionPlan` against all global, venue, and strategy risk limits.
        Fail-closed if any dependency or database check fails.
        """
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        reasons: List[str] = []

        try:
            # 1. Verify database connectivity & kill switch state (CORE-008, CORE-009)
            venue_ids = sorted({leg.venue_id for leg in plan.legs})
            halted, halt_reasons = self.is_halted(plan.strategy_id, venue_ids)
            if halted and not reduce_only:
                reasons.append(RiskRejectCode.KILL_SWITCH_ACTIVE.value)
                reasons.extend(halt_reasons)
        except Exception as exc:
            # Fail-closed when ledger DB is unavailable!
            return RiskDecision(
                cycle_id=plan.cycle_id,
                opportunity_id=plan.opportunity_id,
                strategy_id=plan.strategy_id,
                policy_version=self._policy.policy_version,
                approved=False,
                reason_codes=[RiskRejectCode.RISK_SERVICE_UNAVAILABLE.value, str(exc)],
                evaluated_at_utc=ref_now,
            )

        if account_state is None:
            return RiskDecision(
                cycle_id=plan.cycle_id,
                opportunity_id=plan.opportunity_id,
                strategy_id=plan.strategy_id,
                policy_version=self._policy.policy_version,
                approved=False,
                reason_codes=[RiskRejectCode.RISK_SERVICE_UNAVAILABLE.value, "MISSING_ACCOUNT_STATE"],
                evaluated_at_utc=ref_now,
            )

        # 2. Check live trading gate
        req_mode = ExecutionMode(plan.execution_mode.lower())
        if req_mode == ExecutionMode.LIVE and not self._settings.feature_flags.live_trading_enabled:
            reasons.append(RiskRejectCode.LIVE_TRADING_DISABLED_GATE.value)

        for vid in venue_ids:
            if not self._settings.is_venue_executable(vid, req_mode):
                reasons.append(f"{RiskRejectCode.LIVE_TRADING_DISABLED_GATE.value}:{vid}")

        # 3. Check market health across all legs
        if not market_health_list or any(not mh.validity for mh in market_health_list):
            reasons.append(RiskRejectCode.MARKET_HEALTH_INVALID.value)

        # 4. Check unknown order count threshold (`max_unknown_orders_per_strategy`)
        unknown_cnt = account_state.strategy_unknown_order_count.get(plan.strategy_id, 0)
        if unknown_cnt >= self._policy.max_unknown_orders_per_strategy and not reduce_only:
            reasons.append(RiskRejectCode.UNKNOWN_ORDER_THRESHOLD_EXCEEDED.value)
            self.activate_kill_switch(
                KillSwitchScope.STRATEGY,
                plan.strategy_id,
                f"unknown_order_count={unknown_cnt} >= {self._policy.max_unknown_orders_per_strategy}",
            )

        # 5. Check daily loss & unhedged exposure limits
        strat_loss = account_state.strategy_daily_realized_loss_usdt.get(plan.strategy_id, Decimal("0"))
        if strat_loss >= self._policy.max_daily_realized_loss_strategy and not reduce_only:
            reasons.append(RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED.value)
        if account_state.portfolio_daily_realized_loss_usdt >= self._policy.max_daily_realized_loss_portfolio and not reduce_only:
            reasons.append(RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED.value)

        strat_unhedged = account_state.strategy_unhedged_notional_usdt.get(plan.strategy_id, Decimal("0"))
        if strat_unhedged > self._policy.max_unhedged_exposure_notional and not reduce_only:
            reasons.append(RiskRejectCode.UNHEDGED_EXPOSURE_EXCEEDED.value)

        # 6. Check single-order, cycle, strategy, venue, and portfolio notional limits
        cycle_notional = Decimal("0")
        for leg in plan.legs:
            leg_notional = leg.quantity * leg.limit_price
            if leg_notional > self._policy.max_single_order_notional and not reduce_only:
                reasons.append(RiskRejectCode.SINGLE_ORDER_NOTIONAL_EXCEEDED.value)
            cycle_notional = max(cycle_notional, leg_notional)

        if plan.reserved_amount > self._policy.max_cycle_notional and not reduce_only:
            reasons.append(RiskRejectCode.CYCLE_NOTIONAL_EXCEEDED.value)

        cur_strat_notional = account_state.strategy_active_notional_usdt.get(plan.strategy_id, Decimal("0"))
        if cur_strat_notional + plan.reserved_amount > self._policy.max_strategy_notional and not reduce_only:
            reasons.append(RiskRejectCode.STRATEGY_NOTIONAL_EXCEEDED.value)

        if account_state.portfolio_active_notional_usdt + plan.reserved_amount > self._policy.max_portfolio_notional and not reduce_only:
            reasons.append(RiskRejectCode.PORTFOLIO_NOTIONAL_EXCEEDED.value)

        # 7. Check balance freshness
        for (vid, asset), b_snap in account_state.balances.items():
            age_s = (ref_now - b_snap.event_time_utc).total_seconds()
            if age_s > account_state.max_balance_age_seconds:
                reasons.append(f"{RiskRejectCode.STALE_BALANCE_SNAPSHOT.value}:{vid}:{asset}")

        approved = len(reasons) == 0
        if approved:
            reasons.append(RiskRejectCode.PASS_ALL_GATES.value)

        return RiskDecision(
            cycle_id=plan.cycle_id,
            opportunity_id=plan.opportunity_id,
            strategy_id=plan.strategy_id,
            policy_version=self._policy.policy_version,
            approved=approved,
            reason_codes=reasons,
            evaluated_at_utc=ref_now,
        )

    def reserve_funds(
        self,
        cycle_id: str,
        strategy_id: str,
        venue_id: str,
        asset: str,
        amount: Decimal,
        balance_snapshot: BalanceSnapshot,
    ) -> FundReservation:
        """
        Atomically reserve `amount` of `asset` on `venue_id`.
        Prevents concurrent cycles/strategies from overselling the same free balance (`S1-011`, `PORT-001`).
        """
        if amount <= Decimal("0"):
            raise ValueError("Reservation amount must be > 0")

        asset_key = (venue_id, asset.upper())
        with self._lock:
            already_reserved = self._active_reservations.get(asset_key, Decimal("0"))
            effective_free = balance_snapshot.allocatable_balance - already_reserved
            if amount > effective_free:
                raise RuntimeError(
                    f"{RiskRejectCode.INSUFFICIENT_FREE_BALANCE_FOR_RESERVATION.value}: "
                    f"requested {amount} {asset} on {venue_id}, but only {effective_free} available "
                    f"(free={balance_snapshot.free_balance}, prior_reserved={already_reserved})"
                )

            res = FundReservation(
                cycle_id=cycle_id,
                strategy_id=strategy_id,
                venue_id=venue_id,
                asset=asset.upper(),
                reserved_amount=amount,
                status="ACTIVE",
            )
            self._active_reservations[asset_key] = already_reserved + amount
            self._reservations_by_id[res.reservation_id] = res

            now_str = res.created_at_utc.isoformat()
            with self._ledger.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO fund_reservations
                    (reservation_id, cycle_id, strategy_id, venue_id, asset, reserved_amount, status, created_at_utc, updated_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        res.reservation_id,
                        res.cycle_id,
                        res.strategy_id,
                        res.venue_id,
                        res.asset,
                        str(res.reserved_amount),
                        res.status,
                        now_str,
                        now_str,
                    ),
                )
            return res

    def release_reservation(self, reservation_id: str, final_status: str = "RELEASED") -> None:
        with self._lock:
            res = self._reservations_by_id.get(reservation_id)
            if not res or res.status != "ACTIVE":
                return
            asset_key = (res.venue_id, res.asset)
            cur_amt = self._active_reservations.get(asset_key, Decimal("0"))
            self._active_reservations[asset_key] = max(Decimal("0"), cur_amt - res.reserved_amount)
            res.status = final_status
            with self._ledger.transaction() as cur:
                cur.execute(
                    "UPDATE fund_reservations SET status = ?, updated_at_utc = ? WHERE reservation_id = ?",
                    (final_status, datetime.now(timezone.utc).isoformat(), reservation_id),
                )

    def get_reserved_amount(self, venue_id: str, asset: str) -> Decimal:
        with self._lock:
            return self._active_reservations.get((venue_id, asset.upper()), Decimal("0"))
