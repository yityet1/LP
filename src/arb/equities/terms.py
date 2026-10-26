"""EQ-001: Equity-linked product terms, legal/redemption rights, and comparability registry.

Enforces S3-001 & S3-002 acceptance gates:
- Ticker similarity or statistical correlation alone NEVER qualifies two equity-linked products
  as fungible or hedgeable (`S3-001`).
- Unreviewed or expired products are strictly locked in `research_only = True` (`S3-002`).
- Every automated candidate opportunity must reference an explicitly approved
  `product_terms_version` and `ComparabilityApproval` record.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class EquityProductKind(str, Enum):
    TRADITIONAL_EQUITY = "TRADITIONAL_EQUITY"       # US / Regulated exchange common stock or ETF
    TOKENIZED_EQUITY = "TOKENIZED_EQUITY"           # Backed or synthetic on-chain/CEX equity token
    EQUITY_PERP = "EQUITY_PERP"                     # Synthetic perpetual swap tracking equity index/price


class RedemptionRights(str, Enum):
    DIRECT_PRIMARY_REDEMPTION = "DIRECT_PRIMARY_REDEMPTION"   # 1:1 redeemable for underlying shares
    CASH_SETTLEMENT_ONLY = "CASH_SETTLEMENT_ONLY"             # Cash redeemable with issuer
    SYNTHETIC_NO_REDEMPTION = "SYNTHETIC_NO_REDEMPTION"       # Pure price-tracking derivative, no share claim


class DividendTreatment(str, Enum):
    CASH_PASS_THROUGH = "CASH_PASS_THROUGH"
    REINVESTED_TOKEN_REBASE = "REINVESTED_TOKEN_REBASE"
    INDEX_FUNDING_ADJUSTED = "INDEX_FUNDING_ADJUSTED"
    NONE_UNCOMPENSATED = "NONE_UNCOMPENSATED"


class EquitySettlementCycle(str, Enum):
    T_PLUS_1 = "T+1"
    T_PLUS_2 = "T+2"
    INSTANT_24X7 = "24x7_INSTANT"
    PERP_CONTINUOUS = "PERP_CONTINUOUS"


class EquityTradingSession(str, Enum):
    REGULAR = "REGULAR"               # 09:30 - 16:00 ET
    PRE_MARKET = "PRE_MARKET"         # 04:00 - 09:30 ET
    AFTER_HOURS = "AFTER_HOURS"       # 16:00 - 20:00 ET
    OVERNIGHT_24X5 = "OVERNIGHT_24X5" # Selected ATS 24x5 window
    CONTINUOUS_24X7 = "CONTINUOUS_24X7"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


class CorporateActionType(str, Enum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_SPLIT = "STOCK_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    SPIN_OFF = "SPIN_OFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class CorporateActionEvent(BaseModel):
    action_id: str
    underlying_ticker: str
    action_type: CorporateActionType
    ex_date_utc: datetime
    effective_date_utc: datetime
    adjustment_ratio: Decimal = Decimal("1.0")
    cash_amount_usd: Decimal = Decimal("0")
    freeze_window_hours_before: int = 24
    freeze_window_hours_after: int = 12
    description: str = ""

    class Config:
        frozen = True

    def is_within_freeze_window(self, now_utc: datetime) -> bool:
        delta_before_sec = (self.ex_date_utc - now_utc).total_seconds()
        delta_after_sec = (now_utc - self.effective_date_utc).total_seconds()
        if 0 <= delta_before_sec <= self.freeze_window_hours_before * 3600:
            return True
        if self.ex_date_utc <= now_utc <= self.effective_date_utc:
            return True
        if 0 <= delta_after_sec <= self.freeze_window_hours_after * 3600:
            return True
        return False


class EquityProductTerms(BaseModel):
    """Comprehensive legal, operational, and market structure terms for an equity-linked instrument."""

    instrument_id: str
    venue_id: str
    symbol: str
    underlying_ticker: str
    underlying_isin: str
    product_kind: EquityProductKind
    issuer_name: str
    redemption_rights: RedemptionRights
    dividend_treatment: DividendTreatment
    settlement_cycle: EquitySettlementCycle
    supported_sessions: Tuple[EquityTradingSession, ...]
    short_sale_allowed: bool = False
    requires_borrow_locate: bool = True
    eligible_jurisdictions: Tuple[str, ...] = ("NON_US_QUALIFIED",)
    account_jurisdiction_verified: bool = False
    legal_review_approved: bool = False
    compliance_approver: Optional[str] = None
    approved_at_utc: Optional[datetime] = None
    expires_at_utc: Optional[datetime] = None
    redemption_fee_bps: Decimal = Decimal("0")
    holding_basis_risk_haircut_bps: Decimal = Decimal("15.0")
    research_only: bool = True
    notes: str = ""

    class Config:
        frozen = True

    @property
    def product_terms_version(self) -> str:
        """Deterministic content hash identifying the exact reviewed version of product terms."""
        payload = {
            "instrument_id": self.instrument_id,
            "venue_id": self.venue_id,
            "underlying_ticker": self.underlying_ticker,
            "underlying_isin": self.underlying_isin,
            "product_kind": self.product_kind.value,
            "redemption_rights": self.redemption_rights.value,
            "dividend_treatment": self.dividend_treatment.value,
            "settlement_cycle": self.settlement_cycle.value,
            "short_sale_allowed": self.short_sale_allowed,
            "account_jurisdiction_verified": self.account_jurisdiction_verified,
            "legal_review_approved": self.legal_review_approved,
            "research_only": self.research_only,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        return f"terms_v1_{digest}"

    def is_executable_approved(self, now_utc: datetime) -> Tuple[bool, Optional[str]]:
        if self.research_only:
            return False, "PRODUCT_MARKED_RESEARCH_ONLY"
        if not self.legal_review_approved or not self.compliance_approver:
            return False, "PRODUCT_LEGAL_TERMS_UNAPPROVED"
        if not self.account_jurisdiction_verified:
            return False, "JURISDICTION_ELIGIBILITY_UNVERIFIED"
        if self.expires_at_utc and now_utc >= self.expires_at_utc:
            return False, "PRODUCT_TERMS_APPROVAL_EXPIRED"
        return True, None


class ComparabilityApproval(BaseModel):
    """Explicit compliance & quant approval linking two equity instruments as hedgeable."""

    approval_id: str
    leg_a_instrument_id: str
    leg_b_instrument_id: str
    underlying_isin: str
    allow_cross_market_conversion: bool = False  # True only if primary redemption/issuance is active
    allow_price_convergence_spread: bool = True  # True if statistical/convergence spread is approved
    requires_synchronous_session: bool = True
    max_holding_hours: Decimal = Decimal("72")
    min_convergence_haircut_bps: Decimal = Decimal("10.0")
    approved_by: str
    approved_at_utc: datetime
    research_only: bool = False

    class Config:
        frozen = True


class EquityTermsRegistry:
    """Registry managing EquityProductTerms, ComparabilityApprovals, and CorporateActionEvents."""

    def __init__(self) -> None:
        self._terms: Dict[str, EquityProductTerms] = {}
        self._pair_approvals: Dict[Tuple[str, str], ComparabilityApproval] = {}
        self._corporate_actions: Dict[str, List[CorporateActionEvent]] = {}

    def register_terms(self, terms: EquityProductTerms) -> None:
        self._terms[terms.instrument_id] = terms

    def get_terms(self, instrument_id: str) -> Optional[EquityProductTerms]:
        return self._terms.get(instrument_id)

    def register_comparability(
        self,
        approval: ComparabilityApproval,
        now_utc: Optional[datetime] = None,
    ) -> None:
        now = now_utc or datetime.now(timezone.utc)
        terms_a = self._terms.get(approval.leg_a_instrument_id)
        terms_b = self._terms.get(approval.leg_b_instrument_id)
        if terms_a is None or terms_b is None:
            raise ValueError("Both instruments must be registered in EquityTermsRegistry before approval")
        # S3-001: Ticker similarity alone is never enough; ISIN must match exactly
        if terms_a.underlying_isin != terms_b.underlying_isin or terms_a.underlying_isin != approval.underlying_isin:
            raise ValueError(
                f"S3-001 Violation: Underlying ISIN mismatch ({terms_a.underlying_isin} vs {terms_b.underlying_isin})"
            )
        # If neither instrument has direct redemption rights, cross-market conversion cannot be enabled
        if approval.allow_cross_market_conversion:
            has_redemption = (
                terms_a.redemption_rights == RedemptionRights.DIRECT_PRIMARY_REDEMPTION
                or terms_b.redemption_rights == RedemptionRights.DIRECT_PRIMARY_REDEMPTION
            )
            if not has_redemption:
                raise ValueError(
                    "S3-001 Violation: Cannot enable allow_cross_market_conversion without DIRECT_PRIMARY_REDEMPTION"
                )
        key_ab = (approval.leg_a_instrument_id, approval.leg_b_instrument_id)
        key_ba = (approval.leg_b_instrument_id, approval.leg_a_instrument_id)
        self._pair_approvals[key_ab] = approval
        self._pair_approvals[key_ba] = approval

    def register_corporate_action(self, event: CorporateActionEvent) -> None:
        bucket = self._corporate_actions.setdefault(event.underlying_ticker, [])
        bucket.append(event)

    def get_active_corporate_actions(
        self,
        underlying_ticker: str,
        now_utc: datetime,
    ) -> List[CorporateActionEvent]:
        events = self._corporate_actions.get(underlying_ticker, [])
        return [ev for ev in events if ev.is_within_freeze_window(now_utc)]

    def validate_pair_for_execution(
        self,
        leg_a_instrument_id: str,
        leg_b_instrument_id: str,
        now_utc: datetime,
    ) -> Tuple[bool, Optional[str], Optional[ComparabilityApproval], Optional[EquityProductTerms], Optional[EquityProductTerms]]:
        """Validate whether two instruments can form an executable S3 arbitrage pair."""
        terms_a = self._terms.get(leg_a_instrument_id)
        terms_b = self._terms.get(leg_b_instrument_id)
        if terms_a is None or terms_b is None:
            return False, "UNREGISTERED_EQUITY_PRODUCT_TERMS", None, terms_a, terms_b

        ok_a, reason_a = terms_a.is_executable_approved(now_utc)
        if not ok_a:
            return False, f"LEG_A_{reason_a}", None, terms_a, terms_b

        ok_b, reason_b = terms_b.is_executable_approved(now_utc)
        if not ok_b:
            return False, f"LEG_B_{reason_b}", None, terms_a, terms_b

        approval = self._pair_approvals.get((leg_a_instrument_id, leg_b_instrument_id))
        if approval is None:
            return False, "COMPARABILITY_UNAPPROVED_TICKER_ONLY_FORBIDDEN", None, terms_a, terms_b

        if approval.research_only:
            return False, "COMPARABILITY_PAIR_RESEARCH_ONLY", approval, terms_a, terms_b

        # S3-006: Check corporate action freeze window
        active_ca = self.get_active_corporate_actions(terms_a.underlying_ticker, now_utc)
        if active_ca:
            ca_types = ",".join(ev.action_type.value for ev in active_ca)
            return False, f"CORPORATE_ACTION_FREEZE:{ca_types}", approval, terms_a, terms_b

        return True, None, approval, terms_a, terms_b
