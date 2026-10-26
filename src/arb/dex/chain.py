"""DEX-001: On-chain RPC/Event Adapter, Block Finality & Reorg Detector, and Contract Whitelist.

Enforces S4-A acceptance gates (`S4A-002`, `S4A-005`):
- All on-chain events carry `chain_id`, `block_number`, `block_hash`, `block_timestamp_utc`,
  and `received_timestamp_utc`.
- Stale block tips, multi-RPC node height/hash disagreement, or chain reorgs immediately mark
  the chain unhealthy and block executable opportunity generation.
- Only whitelisted chains, pool contracts, and compliance-reviewed tokens (free of transfer-tax
  or blacklist hooks) are permitted in automated strategies (`S4A-002`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from arb.domain.models import ensure_utc


class BlockHeaderSnapshot(BaseModel):
    chain_id: int
    block_number: int
    block_hash: str
    parent_hash: str
    block_timestamp_utc: datetime
    received_timestamp_utc: datetime
    finalized_block_number: int
    base_fee_per_gas_wei: int
    priority_fee_per_gas_wei: int

    class Config:
        frozen = True


class WhitelistTokenMetadata(BaseModel):
    chain_id: int
    token_address: str
    symbol: str
    decimals: int
    is_whitelisted: bool = False
    has_transfer_tax_or_blacklist_hook: bool = False
    compliance_reviewed: bool = False

    class Config:
        frozen = True

    def is_eligible_for_strategy(self) -> Tuple[bool, Optional[str]]:
        if not self.is_whitelisted or not self.compliance_reviewed:
            return False, f"TOKEN_NOT_WHITELISTED:{self.symbol}"
        if self.has_transfer_tax_or_blacklist_hook:
            return False, f"TOKEN_HAS_TRANSFER_TAX_OR_HOOK:{self.symbol}"
        return True, None


class WhitelistPoolContract(BaseModel):
    chain_id: int
    pool_address: str
    protocol: str
    token0_address: str
    token1_address: str
    fee_bps: Decimal
    is_whitelisted: bool = False

    class Config:
        frozen = True


class RpcNodeHealthReport(BaseModel):
    chain_id: int
    is_healthy: bool
    latest_block_number: int
    finalized_block_number: int
    block_age_seconds: float
    rpc_quorum_consistent: bool
    reorg_detected: bool = False
    reason_codes: Tuple[str, ...] = Field(default_factory=tuple)

    class Config:
        frozen = True


class EvmChainAdapter:
    """DEX-001: Manages whitelisted EVM chain state, multi-RPC quorum health, and reorg detection."""

    def __init__(
        self,
        chain_id: int,
        chain_name: str = "ethereum_mainnet",
        max_block_age_seconds: float = 15.0,
        max_rpc_block_skew: int = 1,
        required_confirmations: int = 2,
    ) -> None:
        self.chain_id = chain_id
        self.chain_name = chain_name
        self.max_block_age_seconds = max_block_age_seconds
        self.max_rpc_block_skew = max_rpc_block_skew
        self.required_confirmations = required_confirmations

        self._tokens: Dict[str, WhitelistTokenMetadata] = {}
        self._pools: Dict[str, WhitelistPoolContract] = {}
        self._block_history: Dict[int, BlockHeaderSnapshot] = {}
        self._rpc_tips: Dict[str, BlockHeaderSnapshot] = {}
        self._reorg_detected: bool = False
        self._last_reorg_reason: Optional[str] = None

    def register_token(self, token: WhitelistTokenMetadata) -> None:
        if token.chain_id != self.chain_id:
            raise ValueError(f"Chain ID mismatch: expected {self.chain_id}, got {token.chain_id}")
        self._tokens[token.token_address.lower()] = token

    def get_token(self, token_address: str) -> Optional[WhitelistTokenMetadata]:
        return self._tokens.get(token_address.lower())

    def register_pool_contract(self, pool: WhitelistPoolContract) -> None:
        if pool.chain_id != self.chain_id:
            raise ValueError(f"Chain ID mismatch: expected {self.chain_id}, got {pool.chain_id}")
        self._pools[pool.pool_address.lower()] = pool

    def get_pool_contract(self, pool_address: str) -> Optional[WhitelistPoolContract]:
        return self._pools.get(pool_address.lower())

    def validate_pool_and_tokens(self, pool_address: str) -> Tuple[bool, Optional[str]]:
        pool = self.get_pool_contract(pool_address)
        if pool is None or not pool.is_whitelisted:
            return False, f"POOL_CONTRACT_NOT_WHITELISTED:{pool_address}"
        t0 = self.get_token(pool.token0_address)
        t1 = self.get_token(pool.token1_address)
        if t0 is None or t1 is None:
            return False, "POOL_TOKEN_METADATA_MISSING"
        ok0, r0 = t0.is_eligible_for_strategy()
        if not ok0:
            return False, r0
        ok1, r1 = t1.is_eligible_for_strategy()
        if not ok1:
            return False, r1
        return True, None

    def ingest_block_header(self, rpc_node_id: str, header: BlockHeaderSnapshot) -> bool:
        """Record a new block header from an RPC endpoint and detect parent-hash or height reorgs."""
        if header.chain_id != self.chain_id:
            raise ValueError(f"Wrong chain_id {header.chain_id} for adapter {self.chain_id}")

        existing_same_height = self._block_history.get(header.block_number)
        if existing_same_height is not None and existing_same_height.block_hash != header.block_hash:
            self._reorg_detected = True
            self._last_reorg_reason = (
                f"CHAIN_REORG_HASH_MISMATCH_AT_{header.block_number}:"
                f"{existing_same_height.block_hash}!={header.block_hash}"
            )

        prev_block = self._block_history.get(header.block_number - 1)
        if prev_block is not None and header.parent_hash != prev_block.block_hash:
            self._reorg_detected = True
            self._last_reorg_reason = (
                f"CHAIN_REORG_PARENT_MISMATCH_AT_{header.block_number}:"
                f"parent={header.parent_hash}!=prev={prev_block.block_hash}"
            )

        self._block_history[header.block_number] = header
        self._rpc_tips[rpc_node_id] = header
        return not self._reorg_detected

    def clear_reorg_flag(self) -> None:
        self._reorg_detected = False
        self._last_reorg_reason = None

    def check_rpc_quorum_and_health(self, now_utc: Optional[datetime] = None) -> RpcNodeHealthReport:
        now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        if not self._rpc_tips:
            return RpcNodeHealthReport(
                chain_id=self.chain_id,
                is_healthy=False,
                latest_block_number=0,
                finalized_block_number=0,
                block_age_seconds=999999.0,
                rpc_quorum_consistent=False,
                reorg_detected=self._reorg_detected,
                reason_codes=("NO_RPC_BLOCK_HEADERS",),
            )

        tips = list(self._rpc_tips.values())
        max_block = max(t.block_number for t in tips)
        min_block = min(t.block_number for t in tips)
        best_header = max(tips, key=lambda t: t.block_number)
        age_sec = max(0.0, (now - best_header.block_timestamp_utc).total_seconds())

        reasons: List[str] = []
        quorum_ok = True

        # Check height skew across RPC endpoints
        if (max_block - min_block) > self.max_rpc_block_skew:
            quorum_ok = False
            reasons.append(f"RPC_BLOCK_HEIGHT_SKEW_EXCEEDED:{max_block - min_block}")

        # Check hash agreement among nodes at the same height
        hashes_by_height: Dict[int, str] = {}
        for t in tips:
            prev_h = hashes_by_height.get(t.block_number)
            if prev_h is not None and prev_h != t.block_hash:
                quorum_ok = False
                reasons.append(f"RPC_SPLIT_BRAIN_HASH_MISMATCH_AT_{t.block_number}")
            hashes_by_height[t.block_number] = t.block_hash

        if self._reorg_detected:
            reasons.append(self._last_reorg_reason or "CHAIN_REORG_DETECTED")

        if age_sec > self.max_block_age_seconds:
            reasons.append(f"STALE_CHAIN_TIP:{age_sec:.1f}s>{self.max_block_age_seconds:.1f}s")

        is_healthy = (len(reasons) == 0) and quorum_ok and (not self._reorg_detected)
        return RpcNodeHealthReport(
            chain_id=self.chain_id,
            is_healthy=is_healthy,
            latest_block_number=best_header.block_number,
            finalized_block_number=best_header.finalized_block_number,
            block_age_seconds=age_sec,
            rpc_quorum_consistent=quorum_ok,
            reorg_detected=self._reorg_detected,
            reason_codes=tuple(reasons),
        )
