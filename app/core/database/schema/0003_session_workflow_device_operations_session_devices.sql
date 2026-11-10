-- Migration 3: session workflow: device operations, session devices, operation results
-- Append-only: never edit a migration that has been applied to a real database;
-- add a new numbered file instead.

ALTER TABLE sessions ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE device_operations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'General',
    description TEXT NOT NULL DEFAULT '',
    payload_format TEXT NOT NULL CHECK (payload_format IN ('hex', 'ascii')),
    payload_text TEXT NOT NULL,
    match_mode TEXT NOT NULL CHECK (match_mode IN ('none', 'any', 'exact', 'prefix', 'contains')),
    expected_text TEXT NOT NULL DEFAULT '',
    timeout_ms INTEGER NOT NULL DEFAULT 2000 CHECK (timeout_ms > 0),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE session_devices (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    peer TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    connected_at REAL NOT NULL,
    disconnected_at REAL,
    rx_bytes INTEGER NOT NULL DEFAULT 0,
    tx_bytes INTEGER NOT NULL DEFAULT 0,
    rx_frames INTEGER NOT NULL DEFAULT 0,
    tx_frames INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_session_devices_session ON session_devices(session_id, peer);

CREATE TABLE operation_results (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    executed_at REAL NOT NULL,
    peer TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    operation_name TEXT NOT NULL,
    match_mode TEXT NOT NULL,
    request BLOB NOT NULL,
    expected BLOB,
    response BLOB,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'timeout', 'error', 'sent')),
    latency_ms REAL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_operation_results_session ON operation_results(session_id, executed_at);
