"""[STRAT-001] Unified Opportunity Lifecycle State Machine, Deduplication, and Rejection Reason Taxonomy."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from arb.domain.models import (
    Opportunity,
    OpportunityDecision,
    OpportunityStatus,
    ensure_utc,
)
from arb.ledger.store import LedgerStore


class InvalidOpportunityTransitionError(RuntimeError):
    """Raised when an illegal OpportunityStatus transition is attempted."""


class OpportunityRejectReason(str, Enum):
    MARKET_HEALTH_INVALID = "MARKET_HEALTH_INVALID"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    CROSS_VENUE_SKEW_EXCEEDED = "CROSS_VENUE_SKEW_EXCEEDED"
    INSTRUMENT_NOT_TRADABLE = "INSTRUMENT_NOT_TRADABLE"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    STEP_OR_MIN_QTY_REJECT = "STEP_OR_MIN_QTY_REJECT"
    MIN_NOTIONAL_NOT_MET = "MIN_NOTIONAL_NOT_MET"
    NON_POSITIVE_NET_PNL = "NON_POSITIVE_NET_PNL"
    BELOW_MIN_NET_EDGE_BPS = "BELOW_MIN_NET_EDGE_BPS"
    DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
    OPPORTUNITY_EXPIRED = "OPPORTUNITY_EXPIRED"
    RISK_LIMIT_REJECTED = "RISK_LIMIT_REJECTED"


class OpportunityLifecycleManager:
    """
    [STRAT-001] Enforces strict Opportunity state machine transitions:
    `DETECTED -> VALIDATED -> COSTED -> RISK_CHECKED -> PAPER / APPROVED -> EXECUTING -> RECONCILING -> SETTLED`
    Any state may transition to `REJECTED`, `EXPIRED`, or `HALTED`.
    Also enforces candidate deduplication by `(strategy_id, path_signature, snapshot_sequence_tuple)`
    and TTL expiration checks.
    """

    ALLOWED_TRANSITIONS: Dict[OpportunityStatus, Set[OpportunityStatus]] = {
        OpportunityStatus.DETECTED: {
            OpportunityStatus.VALIDATED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.VALIDATED: {
            OpportunityStatus.COSTED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.COSTED: {
            OpportunityStatus.RISK_CHECKED,
            OpportunityStatus.PAPER,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.RISK_CHECKED: {
            OpportunityStatus.PAPER,
            OpportunityStatus.APPROVED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.PAPER: {
            OpportunityStatus.EXECUTING,
            OpportunityStatus.SETTLED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.APPROVED: {
            OpportunityStatus.EXECUTING,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.EXECUTING: {
            OpportunityStatus.RECONCILING,
            OpportunityStatus.SETTLED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.RECONCILING: {
            OpportunityStatus.SETTLED,
            OpportunityStatus.HALTED,
        },
        OpportunityStatus.SETTLED: set(),
        OpportunityStatus.REJECTED: set(),
        OpportunityStatus.EXPIRED: set(),
        OpportunityStatus.HALTED: set(),
    }

    def __init__(self, ledger: Optional[LedgerStore] = None) -> None:
        self._ledger = ledger
        self._seen_fingerprints: Set[str] = set()
        self._opportunities: Dict[str, Opportunity] = {}

    @staticmethod
    def build_dedup_fingerprint(opp: Opportunity) -> str:
        leg_sig = "|".join(f"{l.venue_id}:{l.instrument_id}:{l.side.value}" for l in opp.legs)
        snap_sig = ",".join(sorted(opp.input_snapshot_ids))
        return f"{opp.strategy_id}::{opp.strategy_version}::{leg_sig}::{snap_sig}"

    def transition(
        self,
        opp: Opportunity,
        target_status: OpportunityStatus,
        now_utc: Optional[datetime] = None,
        reject_reasons: Optional[List[str]] = None,
    ) -> Opportunity:
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)

        # Automatic TTL check before advancing to any active execution state
        if target_status in (
            OpportunityStatus.VALIDATED,
            OpportunityStatus.COSTED,
            OpportunityStatus.RISK_CHECKED,
            OpportunityStatus.PAPER,
            OpportunityStatus.APPROVED,
            OpportunityStatus.EXECUTING,
        ) and ref_now >= opp.expires_at:
            opp.status = OpportunityStatus.EXPIRED
            opp.decision = OpportunityDecision.EXPIRED
            if OpportunityRejectReason.OPPORTUNITY_EXPIRED.value not in opp.reject_reason_codes:
                opp.reject_reason_codes.append(OpportunityRejectReason.OPPORTUNITY_EXPIRED.value)
            return opp

        allowed = self.ALLOWED_TRANSITIONS.get(opp.status, set())
        if target_status not in allowed:
            raise InvalidOpportunityTransitionError(
                f"Illegal Opportunity transition from {opp.status.value} to {target_status.value}"
            )

        opp.status = target_status
        if target_status == OpportunityStatus.REJECTED:
            opp.decision = OpportunityDecision.REJECT
            if reject_reasons:
                for r in reject_reasons:
                    if r not in opp.reject_reason_codes:
                        opp.reject_reason_codes.append(r)
        elif target_status == OpportunityStatus.EXPIRED:
            opp.decision = OpportunityDecision.EXPIRED
            if OpportunityRejectReason.OPPORTUNITY_EXPIRED.value not in opp.reject_reason_codes:
                opp.reject_reason_codes.append(OpportunityRejectReason.OPPORTUNITY_EXPIRED.value)
        elif target_status == OpportunityStatus.PAPER:
            opp.decision = OpportunityDecision.PAPER
        elif target_status == OpportunityStatus.APPROVED:
            opp.decision = OpportunityDecision.APPROVED

        return opp

    def register_and_persist(
        self,
        opp: Opportunity,
        now_utc: Optional[datetime] = None,
    ) -> Opportunity:
        """Check deduplication and TTL, then persist the candidate into `LedgerStore`."""
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        fp = self.build_dedup_fingerprint(opp)
        if fp in self._seen_fingerprints and opp.status not in (
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
        ):
            opp.status = OpportunityStatus.REJECTED
            opp.decision = OpportunityDecision.REJECT
            if OpportunityRejectReason.DUPLICATE_OPPORTUNITY.value not in opp.reject_reason_codes:
                opp.reject_reason_codes.append(OpportunityRejectReason.DUPLICATE_OPPORTUNITY.value)
        else:
            self._seen_fingerprints.add(fp)

        if ref_now >= opp.expires_at and opp.status not in (
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
        ):
            opp.status = OpportunityStatus.EXPIRED
            opp.decision = OpportunityDecision.EXPIRED
            if OpportunityRejectReason.OPPORTUNITY_EXPIRED.value not in opp.reject_reason_codes:
                opp.reject_reason_codes.append(OpportunityRejectReason.OPPORTUNITY_EXPIRED.value)

        self._opportunities[opp.opportunity_id] = opp
        if self._ledger is not None:
            self._ledger.record_opportunity(opp)
        return opp
