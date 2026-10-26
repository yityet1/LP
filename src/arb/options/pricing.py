"""OPT-002: Black-76 / Black-Scholes Option Valuation, Implied Volatility Solver & Greeks Engine.

Enforces S4-B acceptance gates (`S4B-003`, `S4B-004`):
- Outputs reproducible `OptionValuation` records containing `theoretical_price`, `implied_volatility`,
  `GreeksProfile` (`delta`, `gamma`, `vega`, `theta`), input snapshot references, `model_version`,
  and explicit pricing assumptions.
- Refuses to output executable valuations when option quotes are stale, time to expiry <= 0,
  or implied volatility solver fails to converge (`S4B-003`).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import math
from typing import Optional

from pydantic import BaseModel

from arb.domain.models import OptionType
from arb.options.models import OptionContractSpec, OptionMarketQuote


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


class GreeksProfile(BaseModel):
    delta: Decimal
    gamma: Decimal
    vega_per_1pct: Decimal
    theta_per_day: Decimal

    class Config:
        frozen = True


class OptionValuation(BaseModel):
    instrument_id: str
    is_valid: bool
    reject_reason: Optional[str] = None
    model_name: str = "Black76_European"
    model_version: str = "1.0.0"
    assumptions: str = "Continuous lognormal forward, flat term r, European exercise"
    underlying_forward: Decimal
    strike: Decimal
    time_to_expiry_years: Decimal
    risk_free_rate: Decimal
    implied_volatility_bid: Optional[Decimal] = None
    implied_volatility_ask: Optional[Decimal] = None
    implied_volatility_mid: Optional[Decimal] = None
    theoretical_price: Optional[Decimal] = None
    greeks: Optional[GreeksProfile] = None
    evaluated_at_utc: datetime

    class Config:
        frozen = True


class OptionPricingEngine:
    """OPT-002: Analytical Black-76 option pricer, Newton-Raphson IV solver, and Greeks calculator."""

    model_name: str = "Black76_European"
    model_version: str = "1.0.0"

    def __init__(self, risk_free_rate: Decimal = Decimal("0.04"), max_quote_age_ms: int = 1000) -> None:
        self.risk_free_rate = risk_free_rate
        self.max_quote_age_ms = max_quote_age_ms

    @staticmethod
    def black76_price_and_greeks(
        option_type: OptionType,
        forward: float,
        strike: float,
        tte_years: float,
        rate: float,
        vol: float,
    ) -> tuple[float, float, float, float, float]:
        """Return `(price, delta, gamma, vega_per_1pct, theta_per_day)` under Black-76."""
        if tte_years <= 0.0 or vol <= 0.0 or forward <= 0.0 or strike <= 0.0:
            intrinsic = max(0.0, forward - strike) if option_type == OptionType.CALL else max(0.0, strike - forward)
            return intrinsic, 0.0, 0.0, 0.0, 0.0

        sqrt_t = math.sqrt(tte_years)
        d1 = (math.log(forward / strike) + 0.5 * vol * vol * tte_years) / (vol * sqrt_t)
        d2 = d1 - vol * sqrt_t
        df = math.exp(-rate * tte_years)

        if option_type == OptionType.CALL:
            price = df * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
            delta = df * _norm_cdf(d1)
        else:
            price = df * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))
            delta = -df * _norm_cdf(-d1)

        pdf_d1 = _norm_pdf(d1)
        gamma = df * pdf_d1 / (forward * vol * sqrt_t)
        vega_1pct = df * forward * pdf_d1 * sqrt_t * 0.01
        theta_annual = -(df * forward * pdf_d1 * vol) / (2.0 * sqrt_t) + rate * price
        theta_day = theta_annual / 365.0
        return price, delta, gamma, vega_1pct, theta_day

    @classmethod
    def solve_implied_volatility(
        cls,
        option_type: OptionType,
        target_price: float,
        forward: float,
        strike: float,
        tte_years: float,
        rate: float,
        tol: float = 1e-6,
        max_iter: int = 60,
    ) -> Optional[float]:
        """Solve implied volatility via hybrid Newton-Raphson + bisection."""
        if tte_years <= 0.0 or target_price <= 0.0:
            return None
        df = math.exp(-rate * tte_years)
        intrinsic = df * (
            max(0.0, forward - strike) if option_type == OptionType.CALL else max(0.0, strike - forward)
        )
        if target_price <= intrinsic + 1e-8:
            return None

        low, high = 0.01, 5.00
        vol = 0.60
        for _ in range(max_iter):
            px, _, _, vega_1pct, _ = cls.black76_price_and_greeks(
                option_type, forward, strike, tte_years, rate, vol
            )
            diff = px - target_price
            if abs(diff) < tol:
                return vol
            if diff > 0:
                high = min(high, vol)
            else:
                low = max(low, vol)
            vega_raw = vega_1pct * 100.0
            if vega_raw > 1e-7:
                next_vol = vol - diff / vega_raw
                if low < next_vol < high:
                    vol = next_vol
                else:
                    vol = 0.5 * (low + high)
            else:
                vol = 0.5 * (low + high)
        return vol if abs(high - low) < 1e-3 else None

    def value_option(
        self,
        spec: OptionContractSpec,
        quote: OptionMarketQuote,
        reference_volatility: Optional[Decimal],
        now_utc: datetime,
    ) -> OptionValuation:
        ok_q, q_reason = quote.is_executable_and_fresh(now_utc, self.max_quote_age_ms)
        tte = spec.time_to_expiry_years(now_utc)
        if not ok_q:
            return OptionValuation(
                instrument_id=spec.instrument_id,
                is_valid=False,
                reject_reason=q_reason,
                underlying_forward=quote.underlying_forward_price,
                strike=spec.strike,
                time_to_expiry_years=tte,
                risk_free_rate=self.risk_free_rate,
                evaluated_at_utc=now_utc,
            )
        if tte <= Decimal("0.0001"):
            return OptionValuation(
                instrument_id=spec.instrument_id,
                is_valid=False,
                reject_reason="OPTION_EXPIRED_OR_TOO_CLOSE_TO_EXPIRY",
                underlying_forward=quote.underlying_forward_price,
                strike=spec.strike,
                time_to_expiry_years=tte,
                risk_free_rate=self.risk_free_rate,
                evaluated_at_utc=now_utc,
            )

        assert quote.bid_price is not None and quote.ask_price is not None
        f_val = float(quote.underlying_forward_price)
        k_val = float(spec.strike)
        t_val = float(tte)
        r_val = float(self.risk_free_rate)

        iv_bid = self.solve_implied_volatility(spec.option_type, float(quote.bid_price), f_val, k_val, t_val, r_val)
        iv_ask = self.solve_implied_volatility(spec.option_type, float(quote.ask_price), f_val, k_val, t_val, r_val)
        if iv_bid is None or iv_ask is None:
            return OptionValuation(
                instrument_id=spec.instrument_id,
                is_valid=False,
                reject_reason="IMPLIED_VOLATILITY_SOLVER_FAILED_CONVERGENCE",
                underlying_forward=quote.underlying_forward_price,
                strike=spec.strike,
                time_to_expiry_years=tte,
                risk_free_rate=self.risk_free_rate,
                evaluated_at_utc=now_utc,
            )

        iv_mid = 0.5 * (iv_bid + iv_ask)
        vol_for_theo = float(reference_volatility) if reference_volatility is not None else iv_mid
        theo_px, delta, gamma, vega_1pct, theta_day = self.black76_price_and_greeks(
            spec.option_type, f_val, k_val, t_val, r_val, vol_for_theo
        )

        return OptionValuation(
            instrument_id=spec.instrument_id,
            is_valid=True,
            underlying_forward=quote.underlying_forward_price,
            strike=spec.strike,
            time_to_expiry_years=tte,
            risk_free_rate=self.risk_free_rate,
            implied_volatility_bid=Decimal(f"{iv_bid:.6f}"),
            implied_volatility_ask=Decimal(f"{iv_ask:.6f}"),
            implied_volatility_mid=Decimal(f"{iv_mid:.6f}"),
            theoretical_price=Decimal(f"{theo_px:.4f}"),
            greeks=GreeksProfile(
                delta=Decimal(f"{delta:.6f}"),
                gamma=Decimal(f"{gamma:.8f}"),
                vega_per_1pct=Decimal(f"{vega_1pct:.4f}"),
                theta_per_day=Decimal(f"{theta_day:.4f}"),
            ),
            evaluated_at_utc=now_utc,
        )
