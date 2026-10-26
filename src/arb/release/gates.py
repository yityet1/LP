"""REL-001: Release Gate Checklist, Paper Soak Test Evaluator & Per-Strategy Live Sign-Off.

Enforces Release & Go-Live gates (`G1`, `G2`, `G3`, `G4`):
- Records per-strategy P0/P1 acceptance test pass status, git commit hash, config version hash,
  paper soak duration hours, reconciliation discrepancy count, P0 alert count, multi-role
  sign-offs (`quant_lead`, `risk_lead`, `sre_lead`), and live trading flag status.
- Shared core failures (`core_p0_passed = False`) immediately block ALL strategies from live
  promotion.
- Strategy-specific failures only disable the affected strategy while allowing healthy
  independent strategies to proceed in paper/canary mode.
- Without explicit sign-offs, zero unexplained ledger discrepancies, and required paper soak
  hours, `can_enable_live()` strictly returns `False` (`LIVE_TRADING_ENABLED=false` hard default).
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class StrategySignOffRecord(BaseModel):
    quant_lead_signer: Optional[str] = None
    risk_lead_signer: Optional[str] = None
    sre_lead_signer: Optional[str] = None
    approval_ticket_id: Optional[str] = None
    signed_at_utc: Optional[datetime] = None

    class Config:
        frozen = True

    @property
    def is_fully_signed(self) -> bool:
        return bool(
            self.quant_lead_signer
            and self.risk_lead_signer
            and self.sre_lead_signer
            and self.approval_ticket_id
        )


class StrategyReleaseChecklist(BaseModel):
    strategy_id: str
    strategy_family: str  # "S1" | "S2" | "S3" | "S4-A" | "S4-B" | "S4-C"
    git_commit_sha: str
    config_version_hash: str
    p0_acceptance_passed: bool = False
    p1_acceptance_passed: bool = False
    paper_soak_hours_completed: float = 0.0
    required_paper_soak_hours: float = 72.0
    unexplained_ledger_discrepancies: int = 0
    unhandled_p0_alerts: int = 0
    sign_off: StrategySignOffRecord = Field(default_factory=StrategySignOffRecord)
    live_flag_enabled: bool = False

    class Config:
        frozen = True


class ReleaseGateController:
    """REL-001: Evaluates G1-G4 release gates per strategy and enforces core vs strategy fault isolation."""

    def __init__(self, core_p0_passed: bool = True) -> None:
        self.core_p0_passed = core_p0_passed
        self._checklists: Dict[str, StrategyReleaseChecklist] = {}

    def set_core_p0_status(self, passed: bool) -> None:
        self.core_p0_passed = passed

    def register_checklist(self, checklist: StrategyReleaseChecklist) -> None:
        self._checklists[checklist.strategy_id] = checklist

    def evaluate_strategy_gate(self, strategy_id: str) -> Tuple[bool, List[str]]:
        """
        Evaluate whether `strategy_id` satisfies all G1 (Replay), G2 (Paper Soak),
        G3 (Canary/Risk), and G4 (Sign-Off) gates to enable live execution.
        """
        reasons: List[str] = []
        if not self.core_p0_passed:
            reasons.append("SHARED_CORE_P0_GATE_FAILED_ALL_STRATEGIES_BLOCKED")

        chk = self._checklists.get(strategy_id)
        if chk is None:
            reasons.append("MISSING_STRATEGY_RELEASE_CHECKLIST")
            return False, reasons

        if not chk.p0_acceptance_passed:
            reasons.append("STRATEGY_P0_ACCEPTANCE_FAILED")
        if not chk.p1_acceptance_passed:
            reasons.append("STRATEGY_P1_ACCEPTANCE_FAILED")
        if chk.paper_soak_hours_completed < chk.required_paper_soak_hours:
            reasons.append(
                f"INSUFFICIENT_PAPER_SOAK_HOURS:{chk.paper_soak_hours_completed:.1f}h<{chk.required_paper_soak_hours:.1f}h"
            )
        if chk.unexplained_ledger_discrepancies > 0:
            reasons.append(f"UNEXPLAINED_LEDGER_DISCREPANCIES:{chk.unexplained_ledger_discrepancies}")
        if chk.unhandled_p0_alerts > 0:
            reasons.append(f"UNHANDLED_P0_ALERTS:{chk.unhandled_p0_alerts}")
        if not chk.sign_off.is_fully_signed:
            reasons.append("MISSING_MULTI_ROLE_RELEASE_SIGN_OFF")
        if not chk.live_flag_enabled:
            reasons.append("LIVE_FLAG_DISABLED_BY_DEFAULT")

        return (len(reasons) == 0), reasons
