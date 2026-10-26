"""[CORE-005] Instrument Registry, versioned TradingRules, and Decimal quantization engine."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from arb.domain.models import Instrument, InstrumentType, OrderSide, TradingStatus, ensure_utc


class InstrumentNotTradableError(RuntimeError):
    """Raised when an instrument is missing, halted, or in research-only state."""


class RulesExpiredError(RuntimeError):
    """Raised when instrument trading rules are stale beyond max_rules_age_seconds."""


class QuantizedOrderSpec(BaseModel):
    venue_id: str
    instrument_id: str
    rules_version: str
    quantized_qty: Decimal
    quantized_price: Decimal
    notional_quote: Decimal
    effective_base_exposure: Decimal


class TradingRules(BaseModel):
    venue_id: str
    instrument_id: str
    venue_symbol: str
    instrument_type: InstrumentType
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_multiplier: Decimal
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    min_qty: Decimal
    trading_status: TradingStatus
    updated_at_utc: datetime
    rules_version: str

    @classmethod
    def from_instrument(cls, inst: Instrument) -> "TradingRules":
        payload = {
            "venue_id": inst.venue_id,
            "instrument_id": inst.instrument_id,
            "venue_symbol": inst.venue_symbol,
            "instrument_type": inst.instrument_type.value,
            "base_asset": inst.base_asset,
            "quote_asset": inst.quote_asset,
            "settlement_asset": inst.settlement_asset,
            "contract_multiplier": str(inst.contract_multiplier),
            "tick_size": str(inst.tick_size),
            "step_size": str(inst.step_size),
            "min_notional": str(inst.min_notional),
            "min_qty": str(inst.min_qty),
            "trading_status": inst.trading_status.value,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        return cls(
            venue_id=inst.venue_id,
            instrument_id=inst.instrument_id,
            venue_symbol=inst.venue_symbol,
            instrument_type=inst.instrument_type,
            base_asset=inst.base_asset,
            quote_asset=inst.quote_asset,
            settlement_asset=inst.settlement_asset,
            contract_multiplier=inst.contract_multiplier,
            tick_size=inst.tick_size,
            step_size=inst.step_size,
            min_notional=inst.min_notional,
            min_qty=inst.min_qty,
            trading_status=inst.trading_status,
            updated_at_utc=ensure_utc(inst.receive_time_utc),
            rules_version=f"rules_{digest}",
        )

    def quantize_quantity(self, raw_qty: Decimal) -> Decimal:
        """Strictly floor quantity to step_size using Decimal arithmetic (never float round())."""
        if raw_qty <= Decimal("0"):
            raise ValueError(f"Order quantity must be > 0, got {raw_qty}")
        steps = (raw_qty / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        quantized = steps * self.step_size
        if quantized < self.min_qty or quantized <= Decimal("0"):
            raise InstrumentNotTradableError(
                f"[{self.venue_id}:{self.instrument_id}] Quantized qty {quantized} < min_qty {self.min_qty}"
            )
        return quantized.normalize() if quantized == quantized.to_integral() else quantized

    def quantize_price(self, raw_price: Decimal, side: OrderSide) -> Decimal:
        """Quantize limit price to tick_size: floor for BUY, ceil for SELL to avoid unintended crossing, or exact tick alignment."""
        if raw_price <= Decimal("0"):
            raise ValueError(f"Order price must be > 0, got {raw_price}")
        rounding_mode = ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
        ticks = (raw_price / self.tick_size).to_integral_value(rounding=rounding_mode)
        quantized = ticks * self.tick_size
        if quantized <= Decimal("0"):
            raise InstrumentNotTradableError(
                f"[{self.venue_id}:{self.instrument_id}] Quantized price {quantized} <= 0"
            )
        return quantized

    def validate_and_quantize_order(
        self,
        side: OrderSide,
        raw_qty: Decimal,
        raw_price: Decimal,
        now_utc: Optional[datetime] = None,
        max_rules_age_seconds: int = 3600,
    ) -> QuantizedOrderSpec:
        """Validate trading status, rule freshness, step/tick alignment, contract multiplier, and min_notional."""
        if self.trading_status != TradingStatus.TRADING:
            raise InstrumentNotTradableError(
                f"[{self.venue_id}:{self.instrument_id}] Instrument status is '{self.trading_status.value}', cannot increase risk."
            )
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        age_sec = (ref_now - self.updated_at_utc).total_seconds()
        if age_sec > max_rules_age_seconds:
            raise RulesExpiredError(
                f"[{self.venue_id}:{self.instrument_id}] TradingRules expired (age={age_sec:.1f}s > {max_rules_age_seconds}s)"
            )

        q_qty = self.quantize_quantity(raw_qty)
        q_price = self.quantize_price(raw_price, side)
        effective_base = q_qty * self.contract_multiplier
        notional = effective_base * q_price
        if notional < self.min_notional:
            raise InstrumentNotTradableError(
                f"[{self.venue_id}:{self.instrument_id}] Order notional {notional} < min_notional {self.min_notional}"
            )
        return QuantizedOrderSpec(
            venue_id=self.venue_id,
            instrument_id=self.instrument_id,
            rules_version=self.rules_version,
            quantized_qty=q_qty,
            quantized_price=q_price,
            notional_quote=notional,
            effective_base_exposure=effective_base,
        )


class InstrumentRegistry:
    """Central registry isolating venue-specific symbols and tracking rule versions."""

    def __init__(self, max_rules_age_seconds: int = 3600) -> None:
        self._max_rules_age_seconds = max_rules_age_seconds
        self._instruments: Dict[Tuple[str, str], Instrument] = {}
        self._rules: Dict[Tuple[str, str], TradingRules] = {}
        self._venue_symbol_index: Dict[Tuple[str, str], str] = {}
        self._cross_venue_canonical_map: Dict[str, List[Tuple[str, str]]] = {}

    def register(self, instrument: Instrument, canonical_pair: Optional[str] = None) -> TradingRules:
        key = (instrument.venue_id, instrument.instrument_id)
        rules = TradingRules.from_instrument(instrument)
        instrument.rules_version = rules.rules_version
        self._instruments[key] = instrument
        self._rules[key] = rules
        self._venue_symbol_index[(instrument.venue_id, instrument.venue_symbol)] = instrument.instrument_id
        if canonical_pair:
            bucket = self._cross_venue_canonical_map.setdefault(canonical_pair, [])
            if key not in bucket:
                bucket.append(key)
        return rules

    def get(self, venue_id: str, instrument_id: str) -> Instrument:
        key = (venue_id, instrument_id)
        if key not in self._instruments:
            raise InstrumentNotTradableError(f"Instrument not found in registry: {venue_id}:{instrument_id}")
        return self._instruments[key]

    def get_rules(
        self,
        venue_id: str,
        instrument_id: str,
        now_utc: Optional[datetime] = None,
        require_tradable: bool = True,
    ) -> TradingRules:
        key = (venue_id, instrument_id)
        if key not in self._rules:
            raise InstrumentNotTradableError(f"TradingRules missing for {venue_id}:{instrument_id}")
        rules = self._rules[key]
        if require_tradable and rules.trading_status != TradingStatus.TRADING:
            raise InstrumentNotTradableError(
                f"Instrument {venue_id}:{instrument_id} is not tradable (status={rules.trading_status.value})"
            )
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        age = (ref_now - rules.updated_at_utc).total_seconds()
        if age > self._max_rules_age_seconds:
            raise RulesExpiredError(
                f"TradingRules for {venue_id}:{instrument_id} expired (age={age:.1f}s > {self._max_rules_age_seconds}s)"
            )
        return rules

    def resolve_by_venue_symbol(self, venue_id: str, venue_symbol: str) -> Instrument:
        inst_id = self._venue_symbol_index.get((venue_id, venue_symbol))
        if not inst_id:
            raise InstrumentNotTradableError(f"Unknown venue symbol {venue_symbol} on {venue_id}")
        return self.get(venue_id, inst_id)
