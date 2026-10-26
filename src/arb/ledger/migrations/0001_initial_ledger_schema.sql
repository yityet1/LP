-- [CORE-006] Initial Event Store & Order/Fill Ledger Schema (PostgreSQL & SQLite compatible)
-- Enforces cycle_id correlation, idempotent fill recording, and append-only audit trails.

CREATE TABLE IF NOT EXISTS config_versions (
    config_version_hash TEXT PRIMARY KEY,
    app_env TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    sanitized_config_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    settlement_asset TEXT NOT NULL,
    gross_edge TEXT NOT NULL,
    fees TEXT NOT NULL,
    slippage TEXT NOT NULL,
    expected_net_pnl TEXT NOT NULL,
    net_edge_bps TEXT NOT NULL,
    capacity TEXT NOT NULL,
    reject_reason_codes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    event_time_utc TEXT NOT NULL,
    receive_time_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    state TEXT NOT NULL,
    reserved_asset TEXT NOT NULL,
    reserved_amount TEXT NOT NULL,
    config_version_hash TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(opportunity_id),
    FOREIGN KEY (config_version_hash) REFERENCES config_versions(config_version_hash)
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    approved INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL,
    evaluated_at_utc TEXT NOT NULL,
    FOREIGN KEY (cycle_id) REFERENCES cycles(cycle_id)
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    venue_order_id TEXT,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    native_venue_status TEXT NOT NULL,
    order_quantity TEXT NOT NULL,
    limit_price TEXT,
    cumulative_filled_qty TEXT NOT NULL DEFAULT '0',
    average_fill_price TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (cycle_id) REFERENCES cycles(cycle_id)
);

-- Append-only order state transition audit table
CREATE TABLE IF NOT EXISTS order_events_audit (
    event_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    status TEXT NOT NULL,
    native_venue_status TEXT NOT NULL,
    cumulative_filled_qty TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    event_time_utc TEXT NOT NULL,
    receive_time_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    venue_trade_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    venue_order_id TEXT NOT NULL,
    side TEXT NOT NULL,
    fill_qty TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    fee_amount TEXT NOT NULL,
    fee_asset TEXT NOT NULL,
    event_time_utc TEXT NOT NULL,
    receive_time_utc TEXT NOT NULL,
    UNIQUE(venue_id, venue_trade_id),
    FOREIGN KEY (cycle_id) REFERENCES cycles(cycle_id),
    FOREIGN KEY (client_order_id) REFERENCES orders(client_order_id)
);

CREATE TABLE IF NOT EXISTS fees (
    fee_id TEXT PRIMARY KEY,
    fill_id TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    fee_asset TEXT NOT NULL,
    fee_amount TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    FOREIGN KEY (fill_id) REFERENCES fills(fill_id),
    FOREIGN KEY (cycle_id) REFERENCES cycles(cycle_id)
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    total_balance TEXT NOT NULL,
    free_balance TEXT NOT NULL,
    locked_balance TEXT NOT NULL,
    reserved_balance TEXT NOT NULL,
    in_transit_balance TEXT NOT NULL,
    event_time_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    position_side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    contract_multiplier TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    mark_price TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    margin_used TEXT NOT NULL,
    event_time_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    strategy_id TEXT,
    venue_id TEXT,
    cycle_id TEXT,
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kill_switch_state (
    scope_key TEXT PRIMARY KEY,
    is_halted INTEGER NOT NULL,
    reason TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS fund_reservations (
    reservation_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    reserved_amount TEXT NOT NULL,
    status TEXT NOT NULL, -- ACTIVE | RELEASED | CONSUMED
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_outbox (
    client_order_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT,
    outbox_state TEXT NOT NULL, -- PREPARED | DISPATCHED | RECONCILED | COMPLETED
    worker_id TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_worker_leases (
    cycle_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    acquired_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_discrepancies (
    discrepancy_id TEXT PRIMARY KEY,
    cycle_id TEXT,
    strategy_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    discrepancy_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL, -- OPEN | RESOLVED | MANUAL_INTERVENTION
    created_at_utc TEXT NOT NULL
);
