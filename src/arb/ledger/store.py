"""[CORE-006] Transactional Event Store, Order/Fill Ledger, and Audit Trail."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.config.settings import Settings
from arb.portability import model_to_json, normalize_sqlite_db_path
from arb.domain.models import (
    BalanceSnapshot,
    CycleState,
    ExecutionPlan,
    Fill,
    Opportunity,
    OrderEvent,
    OrderStatus,
    PositionSnapshot,
)


class LedgerError(RuntimeError):
    """Base error for ledger operations."""


class LedgerIntegrityError(LedgerError):
    """Raised when a state regression or un-audited ledger mutation is attempted."""


class RiskDecisionRecord(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"rd_{uuid4().hex[:12]}")
    cycle_id: str
    opportunity_id: str
    strategy_id: str
    policy_version: str
    approved: bool
    reason_codes: List[str] = Field(default_factory=list)
    evaluated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CycleAuditTrail(BaseModel):
    cycle_id: str
    opportunity: Dict[str, Any]
    cycle: Dict[str, Any]
    risk_decisions: List[Dict[str, Any]]
    orders: List[Dict[str, Any]]
    order_audit_events: List[Dict[str, Any]]
    fills: List[Dict[str, Any]]
    fees: List[Dict[str, Any]]


class LedgerStore:
    """
    ACID transactional ledger store backing opportunities, cycles, orders, fills, fees,
    balances, positions, risk_decisions, system_events, and config_versions.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = normalize_sqlite_db_path(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self.apply_migrations()

    def apply_migrations(self) -> None:
        sql_path = Path(__file__).parent / "migrations" / "0001_initial_ledger_schema.sql"
        ddl = sql_path.read_text(encoding="utf-8")
        with self._conn:
            self._conn.executescript(ddl)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE;")
            yield cursor
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()

    def record_config_version(self, settings: Settings) -> str:
        config_hash = settings.compute_config_hash()
        safe_json = json.dumps(settings.to_safe_dict(), sort_keys=True, default=str)
        now_str = datetime.now(timezone.utc).isoformat()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO config_versions
                (config_version_hash, app_env, policy_version, sanitized_config_json, created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    config_hash,
                    settings.app_env.value,
                    settings.risk_policy.policy_version,
                    safe_json,
                    now_str,
                ),
            )
        return config_hash

    def record_opportunity(self, opp: Opportunity) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO opportunities (
                    opportunity_id, strategy_id, strategy_version, status, decision,
                    settlement_asset, gross_edge, fees, slippage, expected_net_pnl,
                    net_edge_bps, capacity, reject_reason_codes_json, payload_json,
                    event_time_utc, receive_time_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opp.opportunity_id,
                    opp.strategy_id,
                    opp.strategy_version,
                    opp.status.value,
                    opp.decision.value,
                    opp.settlement_asset,
                    str(opp.gross_edge),
                    str(opp.fees),
                    str(opp.slippage),
                    str(opp.expected_net_pnl),
                    str(opp.net_edge_bps),
                    str(opp.capacity),
                    json.dumps(opp.reject_reason_codes),
                    model_to_json(opp),
                    opp.event_time_utc.isoformat(),
                    opp.receive_time_utc.isoformat(),
                ),
            )

    def create_cycle(self, plan: ExecutionPlan, risk_decision: RiskDecisionRecord) -> None:
        """Persist ExecutionPlan cycle and associated RiskDecision atomically before any order submission."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO cycles (
                    cycle_id, opportunity_id, strategy_id, strategy_version,
                    execution_mode, state, reserved_asset, reserved_amount,
                    config_version_hash, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.cycle_id,
                    plan.opportunity_id,
                    plan.strategy_id,
                    plan.strategy_version,
                    plan.execution_mode,
                    plan.state.value,
                    plan.reserved_asset,
                    str(plan.reserved_amount),
                    plan.config_version_hash,
                    now_str,
                    now_str,
                ),
            )
            cur.execute(
                """
                INSERT INTO risk_decisions (
                    decision_id, cycle_id, opportunity_id, strategy_id,
                    policy_version, approved, reason_codes_json, evaluated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    risk_decision.decision_id,
                    risk_decision.cycle_id,
                    risk_decision.opportunity_id,
                    risk_decision.strategy_id,
                    risk_decision.policy_version,
                    1 if risk_decision.approved else 0,
                    json.dumps(risk_decision.reason_codes),
                    risk_decision.evaluated_at_utc.isoformat(),
                ),
            )

    def record_order_event(self, event: OrderEvent) -> None:
        """
        Upsert order state while strictly enforcing:
        1. Cumulative filled quantity is monotonically non-decreasing (CORE-005 / EXEC-001).
        2. Every event is appended to `order_events_audit` (never silently overwritten).
        """
        now_str = datetime.now(timezone.utc).isoformat()
        with self.transaction() as cur:
            existing = cur.execute(
                "SELECT cumulative_filled_qty, status FROM orders WHERE client_order_id = ?",
                (event.client_order_id,),
            ).fetchone()

            if existing is None:
                cur.execute(
                    """
                    INSERT INTO orders (
                        client_order_id, cycle_id, strategy_id, venue_id, instrument_id,
                        venue_order_id, side, order_type, status, native_venue_status,
                        order_quantity, limit_price, cumulative_filled_qty, average_fill_price,
                        error_code, error_message, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.client_order_id,
                        event.cycle_id,
                        event.strategy_id,
                        event.venue_id,
                        event.instrument_id,
                        event.venue_order_id,
                        event.side.value,
                        event.order_type.value,
                        event.status.value,
                        event.native_venue_status,
                        str(event.order_quantity),
                        str(event.limit_price) if event.limit_price is not None else None,
                        str(event.cumulative_filled_qty),
                        str(event.average_fill_price) if event.average_fill_price is not None else None,
                        event.error_code,
                        event.error_message,
                        event.event_time_utc.isoformat(),
                        now_str,
                    ),
                )
            else:
                prev_qty = Decimal(str(existing["cumulative_filled_qty"]))
                if event.cumulative_filled_qty < prev_qty:
                    raise LedgerIntegrityError(
                        f"Order {event.client_order_id} cumulative_filled_qty regression "
                        f"forbidden: {event.cumulative_filled_qty} < {prev_qty}"
                    )
                cur.execute(
                    """
                    UPDATE orders
                    SET venue_order_id = COALESCE(?, venue_order_id),
                        status = ?,
                        native_venue_status = ?,
                        cumulative_filled_qty = ?,
                        average_fill_price = COALESCE(?, average_fill_price),
                        error_code = COALESCE(?, error_code),
                        error_message = COALESCE(?, error_message),
                        updated_at_utc = ?
                    WHERE client_order_id = ?
                    """,
                    (
                        event.venue_order_id,
                        event.status.value,
                        event.native_venue_status,
                        str(event.cumulative_filled_qty),
                        str(event.average_fill_price) if event.average_fill_price is not None else None,
                        event.error_code,
                        event.error_message,
                        now_str,
                        event.client_order_id,
                    ),
                )

            cur.execute(
                """
                INSERT INTO order_events_audit (
                    event_id, client_order_id, cycle_id, status, native_venue_status,
                    cumulative_filled_qty, payload_json, event_time_utc, receive_time_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.client_order_id,
                    event.cycle_id,
                    event.status.value,
                    event.native_venue_status,
                    str(event.cumulative_filled_qty),
                    model_to_json(event),
                    event.event_time_utc.isoformat(),
                    event.receive_time_utc.isoformat(),
                ),
            )

    def record_fill(self, fill: Fill) -> bool:
        """
        Idempotently record a fill and its corresponding fee entry.
        Returns True if newly inserted, False if duplicate (venue_id, venue_trade_id) or fill_id was ignored.
        """
        with self.transaction() as cur:
            dup = cur.execute(
                "SELECT 1 FROM fills WHERE fill_id = ? OR (venue_id = ? AND venue_trade_id = ?)",
                (fill.fill_id, fill.venue_id, fill.venue_trade_id),
            ).fetchone()
            if dup is not None:
                return False

            order_row = cur.execute(
                "SELECT cumulative_filled_qty, order_quantity, status FROM orders WHERE client_order_id = ?",
                (fill.client_order_id,),
            ).fetchone()
            if order_row is None:
                raise LedgerIntegrityError(
                    f"Cannot record fill {fill.fill_id} for unknown client_order_id {fill.client_order_id}"
                )

            cur.execute(
                """
                INSERT INTO fills (
                    fill_id, venue_id, venue_trade_id, cycle_id, strategy_id,
                    instrument_id, client_order_id, venue_order_id, side,
                    fill_qty, fill_price, fee_amount, fee_asset,
                    event_time_utc, receive_time_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.venue_id,
                    fill.venue_trade_id,
                    fill.cycle_id,
                    fill.strategy_id,
                    fill.instrument_id,
                    fill.client_order_id,
                    fill.venue_order_id,
                    fill.side.value,
                    str(fill.fill_qty),
                    str(fill.fill_price),
                    str(fill.fee_amount),
                    fill.fee_asset,
                    fill.event_time_utc.isoformat(),
                    fill.receive_time_utc.isoformat(),
                ),
            )

            cur.execute(
                """
                INSERT INTO fees (
                    fee_id, fill_id, cycle_id, venue_id, fee_asset, fee_amount, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fee_{fill.fill_id}",
                    fill.fill_id,
                    fill.cycle_id,
                    fill.venue_id,
                    fill.fee_asset,
                    str(fill.fee_amount),
                    fill.receive_time_utc.isoformat(),
                ),
            )

            new_cum_qty = Decimal(str(order_row["cumulative_filled_qty"])) + fill.fill_qty
            order_qty = Decimal(str(order_row["order_quantity"]))
            new_status = (
                OrderStatus.FILLED.value if new_cum_qty >= order_qty else OrderStatus.PARTIALLY_FILLED.value
            )
            cur.execute(
                """
                UPDATE orders
                SET cumulative_filled_qty = ?,
                    status = ?,
                    updated_at_utc = ?
                WHERE client_order_id = ?
                """,
                (
                    str(new_cum_qty),
                    new_status,
                    datetime.now(timezone.utc).isoformat(),
                    fill.client_order_id,
                ),
            )
            return True

    def record_balance_snapshot(self, snap: BalanceSnapshot) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO balance_snapshots (
                    snapshot_id, venue_id, account_id, asset, total_balance,
                    free_balance, locked_balance, reserved_balance, in_transit_balance, event_time_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap.snapshot_id,
                    snap.venue_id,
                    snap.account_id,
                    snap.asset,
                    str(snap.total_balance),
                    str(snap.free_balance),
                    str(snap.locked_balance),
                    str(snap.reserved_balance),
                    str(snap.in_transit_balance),
                    snap.event_time_utc.isoformat(),
                ),
            )

    def record_position_snapshot(self, pos: PositionSnapshot) -> None:
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO positions (
                    position_id, venue_id, account_id, instrument_id, position_side,
                    quantity, contract_multiplier, entry_price, mark_price,
                    unrealized_pnl, margin_used, event_time_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.position_id,
                    pos.venue_id,
                    pos.account_id,
                    pos.instrument_id,
                    pos.position_side,
                    str(pos.quantity),
                    str(pos.contract_multiplier),
                    str(pos.entry_price),
                    str(pos.mark_price),
                    str(pos.unrealized_pnl),
                    str(pos.margin_used),
                    pos.event_time_utc.isoformat(),
                ),
            )

    def set_kill_switch(self, scope_key: str, is_halted: bool, reason: str, triggered_by: str) -> None:
        """Persist kill switch state across restarts (META-002 / CORE-009)."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self.transaction() as cur:
            cur.execute(
                """
                INSERT INTO kill_switch_state (scope_key, is_halted, reason, triggered_by, updated_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    is_halted = excluded.is_halted,
                    reason = excluded.reason,
                    triggered_by = excluded.triggered_by,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (scope_key, 1 if is_halted else 0, reason, triggered_by, now_str),
            )

    def is_kill_switch_active(self, scope_key: str = "GLOBAL") -> bool:
        row = self._conn.execute(
            "SELECT is_halted FROM kill_switch_state WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        return bool(row and row["is_halted"] == 1)

    def get_cycle_audit_trail(self, cycle_id: str) -> CycleAuditTrail:
        """Retrieve the complete traceable chain from Opportunity -> RiskDecision -> Cycle -> Orders -> Fills -> Fees."""
        cyc = self._conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        if cyc is None:
            raise LedgerError(f"Cycle {cycle_id} not found")
        opp = self._conn.execute(
            "SELECT * FROM opportunities WHERE opportunity_id = ?", (cyc["opportunity_id"],)
        ).fetchone()
        decisions = self._conn.execute(
            "SELECT * FROM risk_decisions WHERE cycle_id = ?", (cycle_id,)
        ).fetchall()
        orders = self._conn.execute("SELECT * FROM orders WHERE cycle_id = ?", (cycle_id,)).fetchall()
        audits = self._conn.execute(
            "SELECT * FROM order_events_audit WHERE cycle_id = ? ORDER BY receive_time_utc ASC",
            (cycle_id,),
        ).fetchall()
        fills = self._conn.execute("SELECT * FROM fills WHERE cycle_id = ?", (cycle_id,)).fetchall()
        fees = self._conn.execute("SELECT * FROM fees WHERE cycle_id = ?", (cycle_id,)).fetchall()

        return CycleAuditTrail(
            cycle_id=cycle_id,
            opportunity=dict(opp) if opp else {},
            cycle=dict(cyc),
            risk_decisions=[dict(r) for r in decisions],
            orders=[dict(o) for o in orders],
            order_audit_events=[dict(a) for a in audits],
            fills=[dict(f) for f in fills],
            fees=[dict(fe) for fe in fees],
        )
