-- Migration 1: initial schema
-- Append-only: never edit a migration that has been applied to a real database;
-- add a new numbered file instead.

CREATE TABLE protocols (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    decoder_type TEXT NOT NULL,
    definition_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 0 CHECK (port BETWEEN 0 AND 65535),
    transport TEXT NOT NULL,
    protocol_name TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE connections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    transport TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 0 CHECK (port BETWEEN 0 AND 65535),
    local_port INTEGER NOT NULL DEFAULT 0 CHECK (local_port BETWEEN 0 AND 65535),
    framing_json TEXT NOT NULL DEFAULT '{"type": "raw"}',
    protocol_name TEXT NOT NULL DEFAULT '',
    timeout_ms INTEGER NOT NULL DEFAULT 5000,
    auto_reconnect INTEGER NOT NULL DEFAULT 0,
    reconnect_interval_ms INTEGER NOT NULL DEFAULT 3000,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_connections_device ON connections(device_id);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE packets (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL,
    timestamp REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('RX', 'TX')),
    source TEXT NOT NULL DEFAULT '',
    destination TEXT NOT NULL DEFAULT '',
    protocol TEXT NOT NULL DEFAULT '',
    length INTEGER NOT NULL,
    data BLOB NOT NULL
);

CREATE INDEX idx_packets_session_time ON packets(session_id, timestamp);

CREATE INDEX idx_packets_connection ON packets(connection_id);

CREATE TABLE test_cases (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL,
    request BLOB NOT NULL,
    expected_response BLOB,
    timeout_ms INTEGER NOT NULL DEFAULT 2000,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE test_results (
    id INTEGER PRIMARY KEY,
    test_case_id INTEGER REFERENCES test_cases(id) ON DELETE CASCADE,
    suite TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    executed_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'error', 'skipped')),
    actual_response BLOB,
    message TEXT NOT NULL DEFAULT '',
    duration_ms REAL NOT NULL DEFAULT 0
);

CREATE INDEX idx_test_results_case ON test_results(test_case_id, executed_at);

CREATE TABLE application_logs (
    id INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_logs_time ON application_logs(timestamp);

CREATE TABLE porting_projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    c_source_root TEXT NOT NULL DEFAULT '',
    python_target_root TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE porting_modules (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES porting_projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    c_source_path TEXT NOT NULL,
    header_paths_json TEXT NOT NULL DEFAULT '[]',
    python_module_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Not Analyzed',
    test_coverage REAL CHECK (test_coverage IS NULL OR test_coverage BETWEEN 0 AND 100),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, c_source_path)
);

CREATE INDEX idx_porting_modules_project ON porting_modules(project_id, status);
