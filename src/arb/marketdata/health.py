"""[MD-004] Unified Market Data Health, Freshness, and Cross-Venue Time Skew Gate."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from arb.config.settings import RiskPolicyConfig
from arb.domain.models import DataHealthStatus, ensure_utc
from arb.marketdata.orderbook import OrderBookBuilder, SequenceStatus
from arb.observability.telemetry import MetricsRegistry


class HealthReasonCode(str, Enum):
    HEALTHY = "HEALTHY"
    BOOK_INVALID = "BOOK_INVALID"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    STREAM_DISCONNECTED = "STREAM_DISCONNECTED"
    EMPTY_SIDES = "EMPTY_SIDES"
    STALE_BOOK_AGE = "STALE_BOOK_AGE"
    CLOCK_DRIFT_FUTURE_EVENT = "CLOCK_DRIFT_FUTURE_EVENT"
    QUEUE_LAG_EXCEEDED = "QUEUE_LAG_EXCEEDED"
    CROSS_VENUE_SKEW_EXCEEDED = "CROSS_VENUE_SKEW_EXCEEDED"


class MarketHealth(BaseModel):
    venue_id: str
    instrument_id: str
    policy_version: str
    validity: bool
    status: DataHealthStatus
    book_age_ms: int
    cross_venue_skew_ms: int = 0
    sequence_status: SequenceStatus
    queue_lag_ms: int = 0
    last_update_id: int
    reason_codes: List[str] = Field(default_factory=list)
    evaluated_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MarketHealthGate:
    """
    [MD-004] Evaluates single-book and multi-book (triangle / cross-venue) market data health.
    Every strategy scanner MUST query `MarketHealthGate` before generating executable candidates.
    All thresholds are controlled by versioned `RiskPolicyConfig`.
    """

    def __init__(
        self,
        risk_policy: RiskPolicyConfig,
        metrics: Optional[MetricsRegistry] = None,
        max_future_clock_drift_ms: int = 200,
    ) -> None:
        self._policy = risk_policy
        self._metrics = metrics or MetricsRegistry()
        self._max_future_clock_drift_ms = max_future_clock_drift_ms

    @property
    def policy_version(self) -> str:
        return self._policy.policy_version

    def evaluate_book(
        self,
        builder: OrderBookBuilder,
        now_utc: Optional[datetime] = None,
        queue_lag_ms: int = 0,
    ) -> MarketHealth:
        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        raw_age_ms = int((ref_now - builder.last_event_time_utc).total_seconds() * 1000)
        book_age_ms = max(0, raw_age_ms)

        reasons: List[str] = []
        status = DataHealthStatus.HEALTHY

        # 1. Sequence & validity check
        if builder.sequence_status == SequenceStatus.GAP_DETECTED:
            reasons.append(HealthReasonCode.SEQUENCE_GAP.value)
            status = DataHealthStatus.SEQUENCE_GAP
        elif builder.sequence_status == SequenceStatus.DISCONNECTED:
            reasons.append(HealthReasonCode.STREAM_DISCONNECTED.value)
            status = DataHealthStatus.DISCONNECTED
        elif not builder.is_valid():
            reasons.append(HealthReasonCode.BOOK_INVALID.value)
            if builder.invalid_reason:
                reasons.append(builder.invalid_reason)
            status = DataHealthStatus.INVALID

        # 2. Check non-empty bid/ask levels
        book_snap = builder.to_order_book(max_levels=1)
        if not book_snap.bids or not book_snap.asks:
            reasons.append(HealthReasonCode.EMPTY_SIDES.value)
            if status == DataHealthStatus.HEALTHY:
                status = DataHealthStatus.INVALID

        # 3. Check clock drift (event timestamp significantly in the future relative to local clock)
        if raw_age_ms < -self._max_future_clock_drift_ms:
            reasons.append(HealthReasonCode.CLOCK_DRIFT_FUTURE_EVENT.value)
            if status == DataHealthStatus.HEALTHY:
                status = DataHealthStatus.INVALID

        # 4. Check book age threshold (`max_book_age_ms`)
        if book_age_ms > self._policy.max_book_age_ms:
            reasons.append(HealthReasonCode.STALE_BOOK_AGE.value)
            if status == DataHealthStatus.HEALTHY:
                status = DataHealthStatus.STALE

        # 5. Check WebSocket queue lag threshold (`max_ws_queue_lag_ms`)
        if queue_lag_ms > self._policy.max_ws_queue_lag_ms:
            reasons.append(HealthReasonCode.QUEUE_LAG_EXCEEDED.value)
            if status == DataHealthStatus.HEALTHY:
                status = DataHealthStatus.STALE

        validity = len(reasons) == 0
        if validity:
            reasons.append(HealthReasonCode.HEALTHY.value)

        self._metrics.set_gauge(
            "arb_market_book_age_ms",
            float(book_age_ms),
            labels={"venue_id": builder.venue_id, "instrument_id": builder.instrument_id},
        )
        self._metrics.set_gauge(
            "arb_market_health_valid",
            1.0 if validity else 0.0,
            labels={"venue_id": builder.venue_id, "instrument_id": builder.instrument_id},
        )

        return MarketHealth(
            venue_id=builder.venue_id,
            instrument_id=builder.instrument_id,
            policy_version=self._policy.policy_version,
            validity=validity,
            status=status,
            book_age_ms=book_age_ms,
            cross_venue_skew_ms=0,
            sequence_status=builder.sequence_status,
            queue_lag_ms=queue_lag_ms,
            last_update_id=builder.last_update_id,
            reason_codes=reasons,
            evaluated_at_utc=ref_now,
        )

    def evaluate_multi_book_gate(
        self,
        builders: List[OrderBookBuilder],
        now_utc: Optional[datetime] = None,
        queue_lags_ms: Optional[Dict[str, int]] = None,
    ) -> Tuple[bool, List[MarketHealth], List[str]]:
        """
        Evaluate a group of order books (e.g., 3 legs for Triangle Arbitrage or 2 legs for Cross-CEX).
        Enforces both individual book health AND `max_cross_venue_skew_ms` across all legs.
        """
        if not builders:
            return False, [], ["EMPTY_BUILDER_LIST"]

        ref_now = ensure_utc(now_utc) if now_utc else datetime.now(timezone.utc)
        lags = queue_lags_ms or {}

        event_times_ms = [int(b.last_event_time_utc.timestamp() * 1000) for b in builders]
        skew_ms = max(event_times_ms) - min(event_times_ms) if len(event_times_ms) > 1 else 0

        health_reports: List[MarketHealth] = []
        aggregate_reasons: List[str] = []
        all_valid = True

        for b in builders:
            lag = lags.get(f"{b.venue_id}:{b.instrument_id}", lags.get(b.venue_id, 0))
            h = self.evaluate_book(b, now_utc=ref_now, queue_lag_ms=lag)
            h.cross_venue_skew_ms = skew_ms

            if skew_ms > self._policy.max_cross_venue_skew_ms:
                if HealthReasonCode.CROSS_VENUE_SKEW_EXCEEDED.value not in h.reason_codes:
                    h.reason_codes = [
                        r for r in h.reason_codes if r != HealthReasonCode.HEALTHY.value
                    ] + [HealthReasonCode.CROSS_VENUE_SKEW_EXCEEDED.value]
                h.validity = False
                if h.status == DataHealthStatus.HEALTHY:
                    h.status = DataHealthStatus.CROSS_VENUE_SKEW

            if not h.validity:
                all_valid = False
                for r in h.reason_codes:
                    tag = f"{b.venue_id}:{b.instrument_id}:{r}"
                    if tag not in aggregate_reasons:
                        aggregate_reasons.append(tag)

            health_reports.append(h)

        return all_valid, health_reports, aggregate_reasons
