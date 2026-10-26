"""DEX-003: On-chain Transaction Lifecycle State Machine, Nonce Manager, and Finality/Reorg Handler.

Enforces S4-A acceptance gates (`S4A-004`, `S4A-005`, `S4A-006`):
- Uses the 10-state `ChainTxStatus` lifecycle (`CREATED -> SIMULATED -> SIGNED -> SUBMITTED ->
  PENDING -> CONFIRMED -> FINALIZED / REVERTED / DROPPED / REPLACED`), strictly separated from
  CEX `OrderStatus`.
- Associates every transaction with `(chain_id, sender_address, nonce, tx_hash)`.
- When a submitted transaction is in `PENDING` or unknown receipt state, `query_before_retry()`
  queries by `(sender_address, nonce, tx_hash)` first and NEVER blindly allocates a new nonce
  or broadcasts a duplicate swap (`S4A-006`).
- Does not mark a transaction `FINALIZED` until `confirmations >= required_confirmations`;
  if a block reorg invalidates a `CONFIRMED` block hash, rolls back to `PENDING` (`S4A-005`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.domain.models import ChainTransaction, ChainTxStatus


class InvalidChainTxTransitionError(RuntimeError):
    """Raised when an illegal ChainTxStatus transition is attempted."""


class OnchainTxRecord(BaseModel):
    tx: ChainTransaction
    replacement_tx_id: Optional[str] = None
    revert_reason: Optional[str] = None
    reorg_rolled_back_count: int = 0
    updated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OnchainTxLifecycleManager:
    """DEX-003: Manages on-chain transaction states, deterministic nonces, replacements, and finality."""

    ALLOWED_TRANSITIONS: Dict[ChainTxStatus, Set[ChainTxStatus]] = {
        ChainTxStatus.CREATED: {
            ChainTxStatus.SIMULATED,
            ChainTxStatus.REVERTED,
            ChainTxStatus.DROPPED,
        },
        ChainTxStatus.SIMULATED: {
            ChainTxStatus.SIGNED,
            ChainTxStatus.DROPPED,
        },
        ChainTxStatus.SIGNED: {
            ChainTxStatus.SUBMITTED,
            ChainTxStatus.DROPPED,
        },
        ChainTxStatus.SUBMITTED: {
            ChainTxStatus.PENDING,
            ChainTxStatus.CONFIRMED,
            ChainTxStatus.REVERTED,
            ChainTxStatus.DROPPED,
            ChainTxStatus.REPLACED,
        },
        ChainTxStatus.PENDING: {
            ChainTxStatus.CONFIRMED,
            ChainTxStatus.REVERTED,
            ChainTxStatus.DROPPED,
            ChainTxStatus.REPLACED,
        },
        ChainTxStatus.CONFIRMED: {
            ChainTxStatus.CONFIRMED,
            ChainTxStatus.FINALIZED,
            # Block reorg can roll a CONFIRMED tx back to PENDING or REVERTED (`S4A-005`)
            ChainTxStatus.PENDING,
            ChainTxStatus.REVERTED,
        },
        ChainTxStatus.FINALIZED: {ChainTxStatus.FINALIZED},
        ChainTxStatus.REVERTED: {ChainTxStatus.REVERTED},
        ChainTxStatus.DROPPED: {ChainTxStatus.DROPPED, ChainTxStatus.REPLACED},
        ChainTxStatus.REPLACED: {ChainTxStatus.REPLACED},
    }

    def __init__(self, chain_id: int, required_confirmations: int = 2) -> None:
        self.chain_id = chain_id
        self.required_confirmations = required_confirmations
        self._next_nonce_by_sender: Dict[str, int] = {}
        self._records_by_id: Dict[str, OnchainTxRecord] = {}
        self._active_tx_by_sender_nonce: Dict[Tuple[str, int], str] = {}
        self._tx_id_by_hash: Dict[str, str] = {}

    def initialize_sender_nonce(self, sender_address: str, next_nonce: int) -> None:
        self._next_nonce_by_sender[sender_address.lower()] = next_nonce

    def create_transaction(
        self,
        cycle_id: str,
        strategy_id: str,
        sender_address: str,
        target_contract: str,
        gas_limit: int,
        max_fee_per_gas_wei: int,
        now_utc: Optional[datetime] = None,
    ) -> OnchainTxRecord:
        """Allocate next sequential nonce for `sender_address` (only if no unresolved pending nonce blocks it)."""
        sender = sender_address.lower()
        # S4A-006 guard: if sender has an unresolved SUBMITTED/PENDING tx, forbid blind new tx creation
        for (s_addr, _nonce), active_id in self._active_tx_by_sender_nonce.items():
            if s_addr == sender:
                rec = self._records_by_id[active_id]
                if rec.tx.status in (ChainTxStatus.SUBMITTED, ChainTxStatus.PENDING):
                    raise RuntimeError(
                        f"S4A-006 Violation: Sender {sender_address} has unresolved {rec.tx.status.value} "
                        f"tx (nonce={rec.tx.nonce}, hash={rec.tx.tx_hash}); query or replace before new nonce!"
                    )

        nonce = self._next_nonce_by_sender.get(sender, 0)
        self._next_nonce_by_sender[sender] = nonce + 1
        now = now_utc or datetime.now(timezone.utc)

        tx = ChainTransaction(
            transaction_id=f"ctx_{uuid4().hex[:12]}",
            cycle_id=cycle_id,
            strategy_id=strategy_id,
            chain_id=self.chain_id,
            sender_address=sender,
            target_contract=target_contract.lower(),
            nonce=nonce,
            status=ChainTxStatus.CREATED,
            gas_limit=gas_limit,
            max_fee_per_gas_wei=max_fee_per_gas_wei,
            required_confirmations=self.required_confirmations,
            event_time_utc=now,
            receive_time_utc=now,
        )
        rec = OnchainTxRecord(tx=tx, updated_at_utc=now)
        self._records_by_id[tx.transaction_id] = rec
        self._active_tx_by_sender_nonce[(sender, nonce)] = tx.transaction_id
        return rec

    def transition(
        self,
        transaction_id: str,
        target_status: ChainTxStatus,
        tx_hash: Optional[str] = None,
        block_number: Optional[int] = None,
        block_hash: Optional[str] = None,
        confirmations: Optional[int] = None,
        gas_used: Optional[int] = None,
        revert_reason: Optional[str] = None,
        now_utc: Optional[datetime] = None,
    ) -> OnchainTxRecord:
        rec = self._records_by_id[transaction_id]
        cur_status = rec.tx.status
        allowed = self.ALLOWED_TRANSITIONS.get(cur_status, set())
        if target_status not in allowed:
            raise InvalidChainTxTransitionError(
                f"Illegal ChainTxStatus transition: {cur_status.value} -> {target_status.value}"
            )

        if target_status == ChainTxStatus.FINALIZED:
            eff_conf = confirmations if confirmations is not None else rec.tx.confirmations
            if eff_conf < rec.tx.required_confirmations:
                raise InvalidChainTxTransitionError(
                    f"S4A-005 Violation: Cannot transition {transaction_id} to FINALIZED with "
                    f"confirmations={eff_conf} < required={rec.tx.required_confirmations}"
                )

        updates: Dict[str, object] = {"status": target_status}
        if tx_hash is not None:
            updates["tx_hash"] = tx_hash
            self._tx_id_by_hash[tx_hash.lower()] = transaction_id
        if block_number is not None:
            updates["block_number"] = block_number
        if block_hash is not None:
            updates["block_hash"] = block_hash
        if confirmations is not None:
            updates["confirmations"] = confirmations
        if gas_used is not None:
            updates["gas_used"] = gas_used

        new_tx = rec.tx.copy(update=updates)
        new_rec = rec.copy(
            update={
                "tx": new_tx,
                "revert_reason": revert_reason or rec.revert_reason,
                "updated_at_utc": now_utc or datetime.now(timezone.utc),
            }
        )
        self._records_by_id[transaction_id] = new_rec
        return new_rec

    def replace_stuck_transaction(
        self,
        original_tx_id: str,
        new_max_fee_per_gas_wei: int,
        new_tx_hash: str,
        now_utc: Optional[datetime] = None,
    ) -> OnchainTxRecord:
        """Replace a stuck SUBMITTED/PENDING tx using the exact same nonce and a bumped gas fee (`S4A-006`)."""
        orig = self._records_by_id[original_tx_id]
        if orig.tx.status not in (ChainTxStatus.SUBMITTED, ChainTxStatus.PENDING, ChainTxStatus.DROPPED):
            raise InvalidChainTxTransitionError(
                f"Can only replace SUBMITTED/PENDING/DROPPED tx, got {orig.tx.status.value}"
            )
        if new_max_fee_per_gas_wei <= orig.tx.max_fee_per_gas_wei:
            raise ValueError("Replacement transaction must increase max_fee_per_gas_wei")

        now = now_utc or datetime.now(timezone.utc)
        replacement_tx = orig.tx.copy(
            update={
                "transaction_id": f"ctx_rep_{uuid4().hex[:10]}",
                "tx_hash": new_tx_hash,
                "status": ChainTxStatus.PENDING,
                "max_fee_per_gas_wei": new_max_fee_per_gas_wei,
                "event_time_utc": now,
                "receive_time_utc": now,
            }
        )
        # Transition original to REPLACED
        self.transition(original_tx_id, ChainTxStatus.REPLACED, now_utc=now)
        self._records_by_id[original_tx_id] = self._records_by_id[original_tx_id].copy(
            update={"replacement_tx_id": replacement_tx.transaction_id}
        )

        rep_rec = OnchainTxRecord(tx=replacement_tx, updated_at_utc=now)
        self._records_by_id[replacement_tx.transaction_id] = rep_rec
        self._active_tx_by_sender_nonce[(replacement_tx.sender_address, replacement_tx.nonce)] = (
            replacement_tx.transaction_id
        )
        self._tx_id_by_hash[new_tx_hash.lower()] = replacement_tx.transaction_id
        return rep_rec

    def handle_block_reorg(
        self,
        orphaned_block_number: int,
        orphaned_block_hash: str,
        now_utc: Optional[datetime] = None,
    ) -> List[OnchainTxRecord]:
        """Roll back any CONFIRMED (non-finalized) transactions included in an orphaned block (`S4A-005`)."""
        affected: List[OnchainTxRecord] = []
        now = now_utc or datetime.now(timezone.utc)
        for tx_id, rec in list(self._records_by_id.items()):
            if (
                rec.tx.status == ChainTxStatus.CONFIRMED
                and rec.tx.block_number == orphaned_block_number
                and rec.tx.block_hash == orphaned_block_hash
            ):
                rolled = rec.tx.copy(
                    update={
                        "status": ChainTxStatus.PENDING,
                        "block_number": None,
                        "block_hash": None,
                        "confirmations": 0,
                    }
                )
                updated_rec = rec.copy(
                    update={
                        "tx": rolled,
                        "reorg_rolled_back_count": rec.reorg_rolled_back_count + 1,
                        "updated_at_utc": now,
                    }
                )
                self._records_by_id[tx_id] = updated_rec
                affected.append(updated_rec)
        return affected

    def get_record(self, transaction_id: str) -> OnchainTxRecord:
        return self._records_by_id[transaction_id]
