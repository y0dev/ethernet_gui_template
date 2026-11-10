-- Migration 2: porting traceability detail
-- Append-only: never edit a migration that has been applied to a real database;
-- add a new numbered file instead.

CREATE TABLE porting_module_dependencies (
    id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES porting_modules(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'include',
    UNIQUE (module_id, depends_on, kind)
);

CREATE TABLE porting_function_mappings (
    id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES porting_modules(id) ON DELETE CASCADE,
    c_symbol TEXT NOT NULL,
    c_kind TEXT NOT NULL DEFAULT 'function',
    python_symbol TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE (module_id, c_symbol, c_kind)
);

CREATE TABLE porting_notes (
    id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES porting_modules(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    category TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE INDEX idx_porting_notes_module ON porting_notes(module_id, created_at);

CREATE TABLE porting_verification_results (
    id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES porting_modules(id) ON DELETE CASCADE,
    executed_at REAL NOT NULL,
    reference_kind TEXT NOT NULL,
    functions_tested_json TEXT NOT NULL DEFAULT '[]',
    passed INTEGER NOT NULL CHECK (passed >= 0),
    failed INTEGER NOT NULL CHECK (failed >= 0),
    binary_compatible INTEGER,
    behavioral_differences TEXT NOT NULL DEFAULT '',
    remaining_risks TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_porting_verif_module ON porting_verification_results(module_id, executed_at);
