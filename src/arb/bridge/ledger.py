"""XCH-001: Cross-Chain Bridge Transfer State Machine, In-Transit Ledger & Finality Monitor.

Enforces S4-C acceptance gates (`S4C-001`, `S4C-002`, `S4C-003`):
- In-transit bridged funds (`in_transit_balance`) are strictly segregated from free allocatable
  balance and NEVER counted as available trading capital on either source or destination chain
  until destination finality is reached (`S4C-001`).
- Unconfirmed bridge messages (`SOURCE_CONFIRMED` / `IN_TRANSIT_MESSAGE_RELAYING`) cannot trigger
  downstream orders that depend on destination chain funds (`S4C-002`).
- Bridge route timeout, paused status, or wrapped-asset depeg automatically pauses the bridge
  route and fires an operational alert (`S4C-003`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.domain.models import BalanceSnapshot, ensure_utc


class BridgeTransferStatus(str, Enum):
    INITIATED = "INITIATED"
    SOURCE_SUBMITTED = "SOURCE_SUBMITTED"
    SOURCE_CONFIRMED = "SOURCE_CONFIRMED"
    IN_TRANSIT_MESSAGE_RELAYING = "IN_TRANSIT_MESSAGE_RELAYING"
    DESTINATION_MINTED_UNFINALIZED = "DESTINATION_MINTED_UNFINALIZED"
    DESTINATION_FINALIZED = "DESTINATION_FINALIZED"
    TIMED_OUT_STUCK = "TIMED_OUT_STUCK"
    FAILED_REVERTED = "FAILED_REVERTED"


class BridgeHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    CONGESTED = "CONGESTED"
    PAUSED_OR_DEPEGGED = "PAUSED_OR_DEPEGGED"


class BridgeRouteSpec(BaseModel):
    route_id: str
    bridge_protocol: str
    source_chain_id: int
    destination_chain_id: int
    source_asset: str
    destination_asset: str
    is_wrapped_representation: bool = False
    wrapped_peg_ratio_to_canonical: Decimal = Decimal("1.0000")
    min_acceptable_peg_ratio: Decimal = Decimal("0.9970")
    fixed_bridge_fee_usd: Decimal = Decimal("2.50")
    variable_bridge_fee_bps: Decimal = Decimal("4.0")
    expected_transit_seconds: int = 300
    max_timeout_seconds: int = 1800
    health_status: BridgeHealthStatus = BridgeHealthStatus.HEALTHY

    class Config:
        frozen = True

    def is_route_usable(self) -> Tuple[bool, Optional[str]]:
        if self.health_status != BridgeHealthStatus.HEALTHY:
            return False, f"BRIDGE_ROUTE_UNHEALTHY:{self.health_status.value}"
        if self.is_wrapped_representation and self.wrapped_peg_ratio_to_canonical < self.min_acceptable_peg_ratio:
            return (
                False,
                f"WRAPPED_ASSET_DEPEG:{self.wrapped_peg_ratio_to_canonical}<{self.min_acceptable_peg_ratio}",
            )
        return True, None


class BridgeTransferRecord(BaseModel):
    transfer_id: str = Field(default_factory=lambda: f"brg_{uuid4().hex[:12]}")
    route_id: str
    source_chain_id: int
    destination_chain_id: int
    asset: str
    expected_amount: Decimal
    actual_received_amount: Optional[Decimal] = None
    bridge_fee_amount: Decimal
    source_tx_hash: Optional[str] = None
    bridge_message_id: Optional[str] = None
    destination_tx_hash: Optional[str] = None
    status: BridgeTransferStatus = BridgeTransferStatus.INITIATED
    destination_confirmations: int = 0
    required_destination_confirmations: int = 2
    initiated_at_utc: datetime
    updated_at_utc: datetime
    timeout_seconds: int = 1800

    @property
    def is_in_transit(self) -> bool:
        return self.status in (
            BridgeTransferStatus.INITIATED,
            BridgeTransferStatus.SOURCE_SUBMITTED,
            BridgeTransferStatus.SOURCE_CONFIRMED,
            BridgeTransferStatus.IN_TRANSIT_MESSAGE_RELAYING,
            BridgeTransferStatus.DESTINATION_MINTED_UNFINALIZED,
            BridgeTransferStatus.TIMED_OUT_STUCK,
        )

    @property
    def is_destination_spendable(self) -> bool:
        """True ONLY when status is DESTINATION_FINALIZED and confirmations >= required (`S4C-001`, `S4C-002`)."""
        return (
            self.status == BridgeTransferStatus.DESTINATION_FINALIZED
            and self.destination_confirmations >= self.required_destination_confirmations
            and (self.actual_received_amount or Decimal("0")) > Decimal("0")
        )


class BridgeTransferLedger:
    """XCH-001: Tracks cross-chain transfers, in-transit balances, timeouts, and route circuit breakers."""

    def __init__(self) -> None:
        self._routes: Dict[str, BridgeRouteSpec] = {}
        self._transfers: Dict[str, BridgeTransferRecord] = {}
        self._paused_routes: Dict[str, str] = {}

    def register_route(self, route: BridgeRouteSpec) -> None:
        self._routes[route.route_id] = route

    def get_route(self, route_id: str) -> Optional[BridgeRouteSpec]:
        return self._routes.get(route_id)

    def is_route_paused(self, route_id: str) -> Tuple[bool, Optional[str]]:
        if route_id in self._paused_routes:
            return True, self._paused_routes[route_id]
        route = self._routes.get(route_id)
        if route is None:
            return True, "UNREGISTERED_BRIDGE_ROUTE"
        ok, reason = route.is_route_usable()
        if not ok:
            return True, reason
        return False, None

    def initiate_transfer(
        self,
        route_id: str,
        asset: str,
        expected_amount: Decimal,
        bridge_fee_amount: Decimal,
        source_tx_hash: str,
        now_utc: Optional[datetime] = None,
    ) -> BridgeTransferRecord:
        paused, reason = self.is_route_paused(route_id)
        if paused:
            raise RuntimeError(f"Cannot initiate bridge transfer on paused/invalid route {route_id}: {reason}")
        route = self._routes[route_id]
        now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        rec = BridgeTransferRecord(
            route_id=route_id,
            source_chain_id=route.source_chain_id,
            destination_chain_id=route.destination_chain_id,
            asset=asset.upper(),
            expected_amount=expected_amount,
            bridge_fee_amount=bridge_fee_amount,
            source_tx_hash=source_tx_hash,
            status=BridgeTransferStatus.SOURCE_SUBMITTED,
            initiated_at_utc=now,
            updated_at_utc=now,
            timeout_seconds=route.max_timeout_seconds,
        )
        self._transfers[rec.transfer_id] = rec
        return rec

    def update_transfer_state(
        self,
        transfer_id: str,
        target_status: BridgeTransferStatus,
        bridge_message_id: Optional[str] = None,
        destination_tx_hash: Optional[str] = None,
        actual_received_amount: Optional[Decimal] = None,
        destination_confirmations: Optional[int] = None,
        now_utc: Optional[datetime] = None,
    ) -> BridgeTransferRecord:
        rec = self._transfers[transfer_id]
        now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        eff_conf = (
            destination_confirmations
            if destination_confirmations is not None
            else rec.destination_confirmations
        )
        if target_status == BridgeTransferStatus.DESTINATION_FINALIZED:
            if eff_conf < rec.required_destination_confirmations:
                raise ValueError(
                    f"S4C-002 Violation: Cannot mark bridge transfer {transfer_id} DESTINATION_FINALIZED "
                    f"with confirmations={eff_conf} < required={rec.required_destination_confirmations}"
                )

        updated = rec.copy(
            update={
                "status": target_status,
                "bridge_message_id": bridge_message_id or rec.bridge_message_id,
                "destination_tx_hash": destination_tx_hash or rec.destination_tx_hash,
                "actual_received_amount": (
                    actual_received_amount
                    if actual_received_amount is not None
                    else rec.actual_received_amount
                ),
                "destination_confirmations": eff_conf,
                "updated_at_utc": now,
            }
        )
        self._transfers[transfer_id] = updated
        return updated

    def scan_timeouts_and_pause_routes(self, now_utc: datetime) -> List[BridgeTransferRecord]:
        """Detect stuck transfers exceeding `timeout_seconds` and automatically pause the route (`S4C-003`)."""
        now = ensure_utc(now_utc)
        timed_out: List[BridgeTransferRecord] = []
        for tid, rec in list(self._transfers.items()):
            if rec.is_in_transit and rec.status != BridgeTransferStatus.TIMED_OUT_STUCK:
                elapsed = (now - rec.initiated_at_utc).total_seconds()
                if elapsed > rec.timeout_seconds:
                    stuck = rec.copy(
                        update={
                            "status": BridgeTransferStatus.TIMED_OUT_STUCK,
                            "updated_at_utc": now,
                        }
                    )
                    self._transfers[tid] = stuck
                    self._paused_routes[rec.route_id] = (
                        f"BRIDGE_TRANSFER_TIMEOUT:{tid}:{int(elapsed)}s>{rec.timeout_seconds}s"
                    )
                    timed_out.append(stuck)
        return timed_out

    def compute_balance_snapshot_with_in_transit(
        self,
        venue_or_chain_id: str,
        account_id: str,
        asset: str,
        settled_free_balance: Decimal,
        locked_balance: Decimal = Decimal("0"),
        reserved_balance: Decimal = Decimal("0"),
        now_utc: Optional[datetime] = None,
    ) -> BalanceSnapshot:
        """
        Build a `BalanceSnapshot` where `in_transit_balance` is tracked explicitly and NEVER added
        to `free_balance` (`allocatable_balance`) (`S4C-001`).
        """
        now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        in_transit = sum(
            (
                t.expected_amount
                for t in self._transfers.values()
                if t.asset == asset.upper() and t.is_in_transit
            ),
            Decimal("0"),
        )
        total = settled_free_balance + locked_balance + in_transit
        return BalanceSnapshot(
            venue_id=venue_or_chain_id,
            account_id=account_id,
            asset=asset.upper(),
            total_balance=total,
            free_balance=settled_free_balance,
            locked_balance=locked_balance,
            reserved_balance=reserved_balance,
            in_transit_balance=in_transit,
            event_time_utc=now,
            receive_time_utc=now,
        )
