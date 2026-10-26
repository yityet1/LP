"""[CORE-002] Environment configuration, secret redaction, and feature flags."""
from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, validator
from arb.portability import compat_root_validator, model_to_json


class ConfigurationError(RuntimeError):
    """Raised when configuration or security gates are violated."""


class AppEnvironment(str, Enum):
    DEV = "dev"
    PAPER = "paper"
    TESTNET = "testnet"
    PRODUCTION = "production"


class ExecutionMode(str, Enum):
    RESEARCH_ONLY = "research_only"
    PAPER = "paper"
    LIVE = "live"


class SecretStr(str):
    """String subclass that permanently masks its value in repr/str/logs."""

    def __new__(cls, value: str = "") -> "SecretStr":
        instance = super().__new__(cls, "***REDACTED***")
        instance._secret_value = value or ""
        return instance

    def get_secret_value(self) -> str:
        return self._secret_value

    def __repr__(self) -> str:
        return "SecretStr('***REDACTED***')"

    def __str__(self) -> str:
        return "***REDACTED***"

    def __bool__(self) -> bool:
        return bool(self._secret_value)


SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|password|passphrase|token|api[-_]?key|private[-_]?key|signature|authorization)",
    re.IGNORECASE,
)


def mask_sensitive_text(text: str, secrets: Optional[List[str]] = None) -> str:
    """Scrub known secret values and common credential patterns from arbitrary log strings."""
    if not text:
        return text
    masked = text
    if secrets:
        for s in secrets:
            if s and len(s) >= 4:
                masked = masked.replace(s, "***REDACTED***")
    masked = re.sub(
        r"(?i)(api[_-]?key|api[_-]?secret|passphrase|signature|authorization)([\s:=]+)([^\s,\"'{}]+)",
        r"\1\2***REDACTED***",
        masked,
    )
    return masked


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive keys or SecretStr instances in a dictionary."""
    redacted: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, SecretStr):
            redacted[k] = "***REDACTED***"
        elif SENSITIVE_KEY_PATTERN.search(str(k)):
            redacted[k] = "***REDACTED***"
        elif isinstance(v, dict):
            redacted[k] = redact_dict(v)
        elif isinstance(v, list):
            redacted[k] = [
                redact_dict(item) if isinstance(item, dict)
                else ("***REDACTED***" if isinstance(item, SecretStr) else item)
                for item in v
            ]
        else:
            redacted[k] = v
    return redacted


class VenueCredentials(BaseModel):
    venue_id: str
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    api_secret: SecretStr = Field(default_factory=lambda: SecretStr(""))
    passphrase: Optional[SecretStr] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {SecretStr: lambda v: "***REDACTED***"}

    @validator("api_key", "api_secret", pre=True)
    def _wrap_secret(cls, v: Any) -> SecretStr:
        if isinstance(v, SecretStr):
            return v
        return SecretStr(str(v or ""))

    @validator("passphrase", pre=True)
    def _wrap_optional_secret(cls, v: Any) -> Optional[SecretStr]:
        if v is None or v == "":
            return None
        if isinstance(v, SecretStr):
            return v
        return SecretStr(str(v))

    def collect_raw_secrets(self) -> List[str]:
        items = [self.api_key.get_secret_value(), self.api_secret.get_secret_value()]
        if self.passphrase:
            items.append(self.passphrase.get_secret_value())
        return [x for x in items if x]


class FeatureFlags(BaseModel):
    live_trading_enabled: bool = False
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    live_approval_ticket_id: Optional[str] = None
    enabled_venues: List[str] = Field(default_factory=lambda: ["binance_spot", "okx_spot"])
    research_only_modules: List[str] = Field(
        default_factory=lambda: [
            "equity_broker",
            "evm_dex",
            "crypto_options",
            "cross_chain_bridge",
        ]
    )


class RiskPolicyConfig(BaseModel):
    policy_version: str
    max_single_order_notional: Decimal
    max_cycle_notional: Decimal
    max_strategy_notional: Decimal
    max_venue_notional: Decimal
    max_portfolio_notional: Decimal
    max_daily_realized_loss_strategy: Decimal
    max_daily_realized_loss_portfolio: Decimal
    max_unhedged_exposure_notional: Decimal
    max_unhedged_duration_ms: int
    max_book_age_ms: int
    max_cross_venue_skew_ms: int
    max_ws_queue_lag_ms: int
    min_net_edge_bps: Decimal
    max_slippage_bps: Decimal
    max_unknown_orders_per_strategy: int
    venue_capabilities: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    @validator(
        "max_single_order_notional",
        "max_cycle_notional",
        "max_strategy_notional",
        "max_venue_notional",
        "max_portfolio_notional",
        "max_daily_realized_loss_strategy",
        "max_daily_realized_loss_portfolio",
        "max_unhedged_exposure_notional",
        "min_net_edge_bps",
        "max_slippage_bps",
    )
    def _must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ConfigurationError(f"Risk limit must be positive, got {v}")
        return v

    @validator("max_book_age_ms", "max_cross_venue_skew_ms", "max_ws_queue_lag_ms")
    def _positive_ms(cls, v: int) -> int:
        if v <= 0:
            raise ConfigurationError(f"Latency threshold must be > 0 ms, got {v}")
        return v

    @classmethod
    def from_yaml(cls, path: Path | str) -> "RiskPolicyConfig":
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigurationError(f"Risk policy file not found: {file_path}")
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        global_gates = raw.get("global_gates", {})
        if global_gates.get("live_trading_enabled", False) is True:
            raise ConfigurationError(
                "Risk policy YAML must NEVER set live_trading_enabled=true by default!"
            )
        notional = raw.get("notional_limits_usdt", {})
        loss = raw.get("loss_and_exposure_limits_usdt", {})
        md_gates = raw.get("market_data_health_gates", {})
        exec_gates = raw.get("execution_and_reconciliation_gates", {})
        try:
            return cls(
                policy_version=str(raw["policy_version"]),
                max_single_order_notional=Decimal(str(notional["max_single_order_notional"])),
                max_cycle_notional=Decimal(str(notional["max_cycle_notional"])),
                max_strategy_notional=Decimal(str(notional["max_strategy_notional"])),
                max_venue_notional=Decimal(str(notional["max_venue_notional"])),
                max_portfolio_notional=Decimal(str(notional["max_portfolio_notional"])),
                max_daily_realized_loss_strategy=Decimal(
                    str(loss["max_daily_realized_loss_strategy"])
                ),
                max_daily_realized_loss_portfolio=Decimal(
                    str(loss["max_daily_realized_loss_portfolio"])
                ),
                max_unhedged_exposure_notional=Decimal(
                    str(loss["max_unhedged_exposure_notional"])
                ),
                max_unhedged_duration_ms=int(loss["max_unhedged_duration_ms"]),
                max_book_age_ms=int(md_gates["max_book_age_ms"]),
                max_cross_venue_skew_ms=int(md_gates["max_cross_venue_skew_ms"]),
                max_ws_queue_lag_ms=int(md_gates["max_ws_queue_lag_ms"]),
                min_net_edge_bps=Decimal(str(exec_gates["min_net_edge_bps"])),
                max_slippage_bps=Decimal(str(exec_gates["max_slippage_bps"])),
                max_unknown_orders_per_strategy=int(
                    exec_gates["max_unknown_orders_per_strategy"]
                ),
                venue_capabilities=raw.get("venue_capabilities", {}),
            )
        except KeyError as exc:
            raise ConfigurationError(f"Missing required risk policy field: {exc}") from exc


class Settings(BaseModel):
    app_env: AppEnvironment = AppEnvironment.PAPER
    log_level: str = "INFO"
    ledger_db_url: str = "sqlite:///:memory:"
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    risk_policy: RiskPolicyConfig
    credentials: Dict[str, VenueCredentials] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True

    @compat_root_validator
    def _enforce_live_trading_gate(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        app_env: AppEnvironment = values.get("app_env", AppEnvironment.PAPER)
        flags: FeatureFlags = values.get("feature_flags")
        risk_policy: Optional[RiskPolicyConfig] = values.get("risk_policy")
        if risk_policy is None:
            raise ConfigurationError("risk_policy is mandatory to initialize Settings.")
        if flags is None:
            return values

        if flags.live_trading_enabled or flags.execution_mode == ExecutionMode.LIVE:
            if app_env != AppEnvironment.PRODUCTION:
                raise ConfigurationError(
                    f"Cannot enable LIVE trading in '{app_env.value}' environment! "
                    "LIVE mode requires APP_ENV='production' and explicit approval gate."
                )
            if not flags.live_approval_ticket_id or len(flags.live_approval_ticket_id.strip()) < 8:
                raise ConfigurationError(
                    "LIVE trading requires a valid 'live_approval_ticket_id' signed by Risk Lead."
                )
            if not flags.live_trading_enabled or flags.execution_mode != ExecutionMode.LIVE:
                raise ConfigurationError(
                    "Both live_trading_enabled=True and execution_mode='live' must match."
                )
        return values

    def is_venue_executable(self, venue_id: str, mode: ExecutionMode) -> bool:
        """Check if a venue is allowed to execute under the requested mode (blocks research_only)."""
        if venue_id in self.feature_flags.research_only_modules:
            return False
        cap = self.risk_policy.venue_capabilities.get(venue_id)
        if not cap:
            return False
        allowed_modes = cap.get("allowed_modes", [])
        if mode.value not in allowed_modes:
            return False
        if mode == ExecutionMode.LIVE and not cap.get("live_approved", False):
            return False
        return True

    def compute_config_hash(self) -> str:
        """Compute deterministic SHA-256 hash of non-secret settings for audit ledger."""
        safe_dict = self.to_safe_dict()
        canonical = json.dumps(safe_dict, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_safe_dict(self) -> Dict[str, Any]:
        """Export settings dictionary with all credentials guaranteed redacted."""
        raw = {
            "app_env": self.app_env.value,
            "log_level": self.log_level,
            "ledger_db_url": mask_sensitive_text(self.ledger_db_url),
            "feature_flags": json.loads(model_to_json(self.feature_flags)),
            "risk_policy": json.loads(model_to_json(self.risk_policy)),
            "credentials": {
                k: {
                    "venue_id": v.venue_id,
                    "api_key": "***REDACTED***",
                    "api_secret": "***REDACTED***",
                    "passphrase": "***REDACTED***" if v.passphrase else None,
                }
                for k, v in self.credentials.items()
            },
        }
        return redact_dict(raw)

    @classmethod
    def from_env(cls, risk_policy_path: Path | str, env_override: Optional[Dict[str, str]] = None) -> "Settings":
        env = {**os.environ, **(env_override or {})}
        app_env = AppEnvironment(env.get("APP_ENV", "paper").lower())
        live_flag_raw = env.get("LIVE_TRADING_ENABLED", "false").strip().lower()
        live_enabled = live_flag_raw in ("true", "1", "yes")
        exec_mode_str = env.get("EXECUTION_MODE", "live" if live_enabled else "paper").lower()
        exec_mode = ExecutionMode(exec_mode_str)
        risk_policy = RiskPolicyConfig.from_yaml(risk_policy_path)

        creds: Dict[str, VenueCredentials] = {}
        if env.get("BINANCE_API_KEY") or env.get("BINANCE_API_SECRET"):
            creds["binance_spot"] = VenueCredentials(
                venue_id="binance_spot",
                api_key=SecretStr(env.get("BINANCE_API_KEY", "")),
                api_secret=SecretStr(env.get("BINANCE_API_SECRET", "")),
            )
        if env.get("OKX_API_KEY") or env.get("OKX_API_SECRET"):
            creds["okx_spot"] = VenueCredentials(
                venue_id="okx_spot",
                api_key=SecretStr(env.get("OKX_API_KEY", "")),
                api_secret=SecretStr(env.get("OKX_API_SECRET", "")),
                passphrase=SecretStr(env.get("OKX_PASSPHRASE", "")) if env.get("OKX_PASSPHRASE") else None,
            )

        return cls(
            app_env=app_env,
            log_level=env.get("LOG_LEVEL", "INFO"),
            ledger_db_url=env.get("LEDGER_DB_URL", "sqlite:///:memory:"),
            feature_flags=FeatureFlags(
                live_trading_enabled=live_enabled,
                execution_mode=exec_mode,
                live_approval_ticket_id=env.get("LIVE_APPROVAL_TICKET_ID") or None,
            ),
            risk_policy=risk_policy,
            credentials=creds,
        )
