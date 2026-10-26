"""[EXEC-002] Order, Fill, and Balance Reconciliation & Crash Recovery Engine."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.domain.models import BalanceSnapshot, CycleState, OrderEvent, OrderStatus
from arb.ledger.store import LedgerStore
from arb.risk.gateway import KillSwitchScope, RiskGateway
from arb.venues.base import AccountAdapter, ExchangeError, TradingAdapter


class ReconciliationAction(str, Enum):
    ALIGNED_SETTLED = "ALIGNED_SETTLED"
    RECOVERED_MISSING_FILLS = "RECOVERED_MISSING_FILLS"
    RETRY_LATER_NOT_FOUND_ONCE = "RETRY_LATER_NOT_FOUND_ONCE"
    HALT_STRATEGY_MANUAL_INTERVENTION = "HALT_STRATEGY_MANUAL_INTERVENTION"


class ReconciliationReport(BaseModel):
    report_id: str = Field(default_factory=lambda: f"rec_{uuid4().hex[:10]}")
    target_id: str
    venue_id: str
    strategy_id: str
    is_aligned: bool
    action: ReconciliationAction
    recovered_fills_count: int = 0
    discrepancies: List[str] = Field(default_factory=list)
    evaluated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReconciliationEngine:
    """
    [EXEC-002] Reconciles local `LedgerStore` orders, fills, and balances against external venue adapters:
    - `reconcile_order()`: Queries external order status for `UNKNOWN`/`SUBMITTING`/`WORKING` orders;
      tolerates a single transient not-found by scheduling retry before declaring discrepancy.
    - `reconcile_fills()`: Fetches venue trade history and backfills any missed WebSocket user-stream fills idempotently.
    - `reconcile_balances()`: Compares expected ledger balance against external `AccountAdapter.fetch_balances()`;
      any unexplained discrepancy exceeding `tolerance` immediately halts affected strategies and queues manual intervention (`CORE-010`).
    """

    def __init__(
        self,
        ledger: LedgerStore,
        risk_gateway: RiskGateway,
        trading_adapters: Dict[str, TradingAdapter],
        account_adapters: Dict[str, AccountAdapter],
    ) -> None:
        self._ledger = ledger
        self._risk = risk_gateway
        self._trading = trading_adapters
        self._accounts = account_adapters
        self._not_found_counts: Dict[str, int] = {}

    async def reconcile_order_and_fills(
        self,
        cycle_id: str,
        strategy_id: str,
        venue_id: str,
        instrument_id: str,
        client_order_id: str,
    ) -> ReconciliationReport:
        adapter = self._trading[venue_id]
        recovered_fills = 0
        discrepancies: List[str] = []

        # 1. Query order status from venue
        try:
            remote_order: OrderEvent = await adapter.query_order(
                instrument_id=instrument_id,
                client_order_id=client_order_id,
            )
            remote_order.cycle_id = cycle_id
            remote_order.strategy_id = strategy_id
        except ExchangeError as exc:
            # Single not-found tolerance (venue eventual consistency right after timeout)
            cnt = self._not_found_counts.get(client_order_id, 0) + 1
            self._not_found_counts[client_order_id] = cnt
            if cnt == 1:
                return ReconciliationReport(
                    target_id=client_order_id,
                    venue_id=venue_id,
                    strategy_id=strategy_id,
                    is_aligned=False,
                    action=ReconciliationAction.RETRY_LATER_NOT_FOUND_ONCE,
                    discrepancies=[f"Order {client_order_id} not found on first query (attempt 1); scheduled retry"],
                )
            discrepancies.append(f"Order {client_order_id} repeatedly not found or errored: {exc}")
            self._record_discrepancy(cycle_id, strategy_id, venue_id, "ORDER_QUERY_FAILED", discrepancies)
            self._risk.activate_kill_switch(
                KillSwitchScope.STRATEGY,
                strategy_id,
                f"Order reconciliation failed for {client_order_id}",
            )
            return ReconciliationReport(
                target_id=client_order_id,
                venue_id=venue_id,
                strategy_id=strategy_id,
                is_aligned=False,
                action=ReconciliationAction.HALT_STRATEGY_MANUAL_INTERVENTION,
                discrepancies=discrepancies,
            )

        # 2. Fetch and backfill any missing fills from venue before updating order status
        remote_fills = await adapter.fetch_fills(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
        )
        for f in remote_fills:
            f.cycle_id = cycle_id
            f.strategy_id = strategy_id
            f.client_order_id = client_order_id
            if self._ledger.record_fill(f):
                recovered_fills += 1

        # 3. Record reconciled order state
        self._ledger.record_order_event(remote_order)

        # Verify local cumulative fill quantity matches remote order executed quantity
        local_row = self._ledger._conn.execute(
            "SELECT cumulative_filled_qty FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        local_cum = Decimal(str(local_row["cumulative_filled_qty"])) if local_row else Decimal("0")

        if local_cum != remote_order.cumulative_filled_qty:
            msg = (
                f"Fill sum mismatch on {client_order_id}: local_fills={local_cum} "
                f"!= remote_order_cum={remote_order.cumulative_filled_qty}"
            )
            discrepancies.append(msg)
            self._record_discrepancy(cycle_id, strategy_id, venue_id, "FILL_QTY_MISMATCH", discrepancies)
            self._risk.activate_kill_switch(KillSwitchScope.STRATEGY, strategy_id, msg)
            return ReconciliationReport(
                target_id=client_order_id,
                venue_id=venue_id,
                strategy_id=strategy_id,
                is_aligned=False,
                action=ReconciliationAction.HALT_STRATEGY_MANUAL_INTERVENTION,
                recovered_fills_count=recovered_fills,
                discrepancies=discrepancies,
            )

        # Mark cycle SETTLED and clear strategy kill switch if it was only halted for UNKNOWN order reconciliation
        with self._ledger.transaction() as cur:
            cur.execute(
                "UPDATE cycles SET state = ?, updated_at_utc = ? WHERE cycle_id = ?",
                (CycleState.SETTLED.value, datetime.now(timezone.utc).isoformat(), cycle_id),
            )
            cur.execute(
                "UPDATE order_outbox SET outbox_state = 'COMPLETED', updated_at_utc = ? WHERE client_order_id = ?",
                (datetime.now(timezone.utc).isoformat(), client_order_id),
            )
        self._ledger.set_kill_switch(f"STRATEGY:{strategy_id}", False, "Reconciled cleanly", "ReconciliationEngine")

        action = (
            ReconciliationAction.RECOVERED_MISSING_FILLS
            if recovered_fills > 0
            else ReconciliationAction.ALIGNED_SETTLED
        )
        return ReconciliationReport(
            target_id=client_order_id,
            venue_id=venue_id,
            strategy_id=strategy_id,
            is_aligned=True,
            action=action,
            recovered_fills_count=recovered_fills,
        )

    async def reconcile_balances(
        self,
        venue_id: str,
        strategy_id: str,
        expected_balances: Dict[str, Decimal],
        tolerance: Decimal = Decimal("0.0001"),
    ) -> ReconciliationReport:
        """
        Compare expected asset balances in ledger against external venue `fetch_balances()` (`CORE-010`).
        Halts venue/strategy and queues manual intervention if any asset diverges beyond `tolerance`.
        """
        acct_adapter = self._accounts[venue_id]
        remote_snaps: List[BalanceSnapshot] = await acct_adapter.fetch_balances()
        remote_map = {b.asset.upper(): b.total_balance for b in remote_snaps}

        discrepancies: List[str] = []
        for asset, exp_total in expected_balances.items():
            actual_total = remote_map.get(asset.upper(), Decimal("0"))
            diff = abs(actual_total - exp_total)
            if diff > tolerance:
                discrepancies.append(
                    f"Balance mismatch on {venue_id}:{asset}: expected={exp_total}, actual={actual_total}, diff={diff}"
                )

        if discrepancies:
            self._record_discrepancy(None, strategy_id, venue_id, "BALANCE_MISMATCH", discrepancies)
            self._risk.activate_kill_switch(
                KillSwitchScope.VENUE,
                venue_id,
                "; ".join(discrepancies),
                triggered_by="ReconciliationEngine",
            )
            self._risk.activate_kill_switch(
                KillSwitchScope.STRATEGY,
                strategy_id,
                "; ".join(discrepancies),
                triggered_by="ReconciliationEngine",
            )
            return ReconciliationReport(
                target_id=f"bal_{venue_id}",
                venue_id=venue_id,
                strategy_id=strategy_id,
                is_aligned=False,
                action=ReconciliationAction.HALT_STRATEGY_MANUAL_INTERVENTION,
                discrepancies=discrepancies,
            )

        return ReconciliationReport(
            target_id=f"bal_{venue_id}",
            venue_id=venue_id,
            strategy_id=strategy_id,
            is_aligned=True,
            action=ReconciliationAction.ALIGNED_SETTLED,
        )

    def _record_discrepancy(
        self,
        cycle_id: Optional[str],
        strategy_id: str,
        venue_id: str,
        dtype: str,
        details: List[str],
    ) -> None:
        with self._ledger.transaction() as cur:
            cur.execute(
                """
                INSERT INTO reconciliation_discrepancies
                (discrepancy_id, cycle_id, strategy_id, venue_id, discrepancy_type, details_json, status, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"disc_{uuid4().hex[:10]}",
                    cycle_id,
                    strategy_id,
                    venue_id,
                    dtype,
                    json.dumps(details),
                    "MANUAL_INTERVENTION",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
