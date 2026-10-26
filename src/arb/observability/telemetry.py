"""[CORE-007] Structured JSON logging, secret scrubbing, Prometheus metrics, and P0/P1 Alert rules."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, Field

from arb.config.settings import mask_sensitive_text, redact_dict


class AlertSeverity(str, Enum):
    P0 = "P0"  # Immediate funds/ledger/unknown-order critical alert
    P1 = "P1"  # Degraded stream / rate-limit backoff / single-strategy warning
    P2 = "P2"  # Informational operational event


class TraceContext(BaseModel):
    correlation_id: str = Field(default_factory=lambda: f"corr_{uuid4().hex[:12]}")
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    span_id: str = Field(default_factory=lambda: uuid4().hex[:16])
    strategy_id: str = "system"
    venue_id: str = "global"
    cycle_id: str = "none"
    client_order_id: str = "none"


class StructuredLogger:
    """
    Emits structured JSON logs containing mandatory correlation fields:
    strategy_id, venue_id, cycle_id, client_order_id, correlation_id.
    Automatically scrubs credentials and signatures.
    """

    REQUIRED_FIELDS = (
        "strategy_id",
        "venue_id",
        "cycle_id",
        "client_order_id",
        "correlation_id",
    )

    def __init__(self, known_secrets: Optional[List[str]] = None) -> None:
        self._known_secrets = known_secrets or []
        self._records: List[Dict[str, Any]] = []

    def log(
        self,
        level: str,
        event: str,
        ctx: TraceContext,
        **extra: Any,
    ) -> str:
        safe_extra = redact_dict(extra)
        payload: Dict[str, Any] = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "event": mask_sensitive_text(event, self._known_secrets),
            "strategy_id": ctx.strategy_id,
            "venue_id": ctx.venue_id,
            "cycle_id": ctx.cycle_id,
            "client_order_id": ctx.client_order_id,
            "correlation_id": ctx.correlation_id,
            "trace_id": ctx.trace_id,
            "span_id": ctx.span_id,
        }
        for k, v in safe_extra.items():
            if isinstance(v, str):
                payload[k] = mask_sensitive_text(v, self._known_secrets)
            else:
                payload[k] = v

        self._records.append(payload)
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return serialized

    @property
    def records(self) -> List[Dict[str, Any]]:
        return list(self._records)


class MetricsRegistry:
    """Prometheus-compatible in-memory counter/gauge registry for platform observability."""

    def __init__(self) -> None:
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        self._gauges: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}

    @staticmethod
    def _norm_labels(labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
        if not labels:
            return ()
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    def inc_counter(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        key = (name, self._norm_labels(labels))
        self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = (name, self._norm_labels(labels))
        self._gauges[key] = value

    def get_counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        return self._counters.get((name, self._norm_labels(labels)), 0.0)

    def get_gauge(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        return self._gauges.get((name, self._norm_labels(labels)), 0.0)

    def export_prometheus_text(self) -> str:
        lines: List[str] = []
        for (name, labels), val in sorted(self._counters.items()):
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            suffix = f"{{{label_str}}}" if label_str else ""
            lines.append(f"{name}{suffix} {val}")
        for (name, labels), val in sorted(self._gauges.items()):
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            suffix = f"{{{label_str}}}" if label_str else ""
            lines.append(f"{name}{suffix} {val}")
        return "\n".join(lines)


class AlertEvent(BaseModel):
    alert_id: str = Field(default_factory=lambda: f"alt_{uuid4().hex[:10]}")
    rule_name: str
    severity: AlertSeverity
    owner: str
    runbook_url: str
    strategy_id: str
    venue_id: str
    cycle_id: str
    summary: str
    triggered_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AlertManager:
    """Evaluates P0/P1 operational conditions and links every alert to an owner and runbook."""

    ALERT_CATALOG: Dict[str, Dict[str, str]] = {
        "UNKNOWN_ORDER_DETECTED": {
            "severity": AlertSeverity.P0.value,
            "owner": "Gemini (Execution Lead)",
            "runbook_url": "docs/runbooks/p0-unknown-order-reconciliation.md",
        },
        "LEDGER_RECONCILIATION_MISMATCH": {
            "severity": AlertSeverity.P0.value,
            "owner": "Gemini (Risk & Data Lead)",
            "runbook_url": "docs/runbooks/p0-ledger-mismatch.md",
        },
        "VENUE_IP_BAN_OR_418": {
            "severity": AlertSeverity.P0.value,
            "owner": "Gemini (Venue Adapter Lead)",
            "runbook_url": "docs/runbooks/p0-venue-rate-limit-ban.md",
        },
        "MARKET_DATA_STREAM_DISCONNECTED": {
            "severity": AlertSeverity.P1.value,
            "owner": "Gemini (Market Data Lead)",
            "runbook_url": "docs/runbooks/p1-market-data-disconnect.md",
        },
        "VENUE_HTTP_429_RATE_LIMITED": {
            "severity": AlertSeverity.P1.value,
            "owner": "Gemini (Venue Adapter Lead)",
            "runbook_url": "docs/runbooks/p1-rate-limit-backoff.md",
        },
    }

    def __init__(self, metrics: MetricsRegistry, logger: StructuredLogger) -> None:
        self._metrics = metrics
        self._logger = logger
        self._fired_alerts: List[AlertEvent] = []

    def trigger(self, rule_name: str, summary: str, ctx: TraceContext) -> AlertEvent:
        spec = self.ALERT_CATALOG.get(
            rule_name,
            {
                "severity": AlertSeverity.P0.value,
                "owner": "Gemini (Chief Engineer)",
                "runbook_url": "docs/runbooks/default-incident.md",
            },
        )
        alert = AlertEvent(
            rule_name=rule_name,
            severity=AlertSeverity(spec["severity"]),
            owner=spec["owner"],
            runbook_url=spec["runbook_url"],
            strategy_id=ctx.strategy_id,
            venue_id=ctx.venue_id,
            cycle_id=ctx.cycle_id,
            summary=summary,
        )
        self._fired_alerts.append(alert)
        self._metrics.inc_counter(
            "arb_alerts_total",
            labels={
                "rule": rule_name,
                "severity": alert.severity.value,
                "venue_id": ctx.venue_id,
            },
        )
        self._logger.log(
            level="CRITICAL" if alert.severity == AlertSeverity.P0 else "WARNING",
            event=f"ALERT_FIRED:{rule_name}",
            ctx=ctx,
            alert_id=alert.alert_id,
            severity=alert.severity.value,
            owner=alert.owner,
            runbook_url=alert.runbook_url,
            summary=summary,
        )
        return alert

    @property
    def fired_alerts(self) -> List[AlertEvent]:
        return list(self._fired_alerts)
