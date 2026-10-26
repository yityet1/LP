"""OPT-001: Option Contract Metadata, Exercise/Settlement Style, and Option Quote Adapter.

Enforces S4-B acceptance gates (`S4B-001`, `S4B-002`):
- Preserves all option-specific contract attributes (`underlying_symbol`, `expiry_utc`, `strike`,
  `option_type`, `contract_multiplier`, `settlement_asset`, `exercise_style`, `settlement_style`)
  and NEVER strips them into a plain spot schema (`S4B-001`).
- Enforces venue/account option trading eligibility (`account_options_approved`) and quote
  freshness (`max_quote_age_ms`), separating executable option `bid`/`ask` from `mark_price` (`S4B-002`).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

from pydantic import BaseModel, validator

from arb.domain.models import OptionType, ensure_utc


class ExerciseStyle(str, Enum):
    EUROPEAN = "EUROPEAN"
    AMERICAN = "AMERICAN"


class OptionSettlementStyle(str, Enum):
    CASH_USD = "CASH_USD"           # Linear USD/USDT cash settled (e.g. OKX / Binance USDT options)
    INVERSE_COIN = "INVERSE_COIN"   # Coin-margined / inverse settled (e.g. Deribit BTC/ETH options)
    PHYSICAL_DELIVERY = "PHYSICAL_DELIVERY"


class OptionContractSpec(BaseModel):
    venue_id: str
    instrument_id: str
    venue_symbol: str
    underlying_symbol: str
    expiry_utc: datetime
    strike: Decimal
    option_type: OptionType
    contract_multiplier: Decimal = Decimal("1")
    settlement_asset: str = "USDT"
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_style: OptionSettlementStyle = OptionSettlementStyle.CASH_USD
    tick_size: Decimal = Decimal("0.5")
    step_size: Decimal = Decimal("0.1")
    min_qty: Decimal = Decimal("0.1")
    account_options_approved: bool = False
    research_only: bool = True

    class Config:
        frozen = True

    @validator("expiry_utc", pre=True)
    def _ensure_utc_expiry(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @validator("strike", "contract_multiplier", "tick_size", "step_size")
    def _positive_fields(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError(f"Option specification field must be > 0, got {v}")
        return v

    def time_to_expiry_years(self, now_utc: datetime) -> Decimal:
        rem_sec = (self.expiry_utc - ensure_utc(now_utc)).total_seconds()
        if rem_sec <= 0:
            return Decimal("0")
        return Decimal(str(rem_sec)) / Decimal(str(365.0 * 86400.0))


class OptionMarketQuote(BaseModel):
    venue_id: str
    instrument_id: str
    bid_price: Optional[Decimal] = None
    bid_size: Decimal = Decimal("0")
    ask_price: Optional[Decimal] = None
    ask_size: Decimal = Decimal("0")
    mark_price: Optional[Decimal] = None
    underlying_index_price: Decimal
    underlying_forward_price: Decimal
    open_interest: Decimal = Decimal("0")
    source_timestamp_utc: datetime
    received_timestamp_utc: datetime

    class Config:
        frozen = True

    def is_executable_and_fresh(self, now_utc: datetime, max_quote_age_ms: int = 1000) -> Tuple[bool, Optional[str]]:
        age_ms = int((ensure_utc(now_utc) - ensure_utc(self.source_timestamp_utc)).total_seconds() * 1000)
        if age_ms > max_quote_age_ms:
            return False, f"STALE_OPTION_QUOTE:{age_ms}ms>{max_quote_age_ms}ms"
        if self.bid_price is None or self.ask_price is None:
            return False, "MISSING_EXECUTABLE_OPTION_BID_OR_ASK"
        if self.bid_price <= Decimal("0") or self.ask_price <= Decimal("0") or self.bid_price >= self.ask_price:
            return False, "INVALID_OR_CROSSED_OPTION_BOOK"
        if self.bid_size <= Decimal("0") or self.ask_size <= Decimal("0"):
            return False, "ZERO_OPTION_TOP_DEPTH"
        return True, None
