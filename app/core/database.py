from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.core.utils import ensure_parent_dir


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    connector TEXT NOT NULL,
    action_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    rationale TEXT,
    policy_notes_json TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    side_effecting INTEGER NOT NULL,
    requires_approval INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider_name TEXT,
    summary_id TEXT,
    created_by_agent_id TEXT,
    created_by_agent_role TEXT,
    reviewed_by_agent_id TEXT,
    reviewed_by_agent_role TEXT,
    correlation_id TEXT,
    data_classification TEXT NOT NULL DEFAULT 'external-ok',
    snapshot_hash TEXT,
    stale_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    snapshot_hash TEXT,
    action_hash TEXT,
    policy_hash TEXT,
    settings_hash TEXT,
    resource_hash TEXT,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS action_history (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error_text TEXT,
    correlation_id TEXT,
    FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    collected_json TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_jobs (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    queued_by TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    worker_id TEXT,
    result_json TEXT,
    error_text TEXT,
    lease_expires_at TEXT,
    last_heartbeat_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT,
    approval_id TEXT,
    dead_letter_reason TEXT,
    FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS audit_entries (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT,
    entry_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_snapshots (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    settings_hash TEXT NOT NULL,
    resource_hash TEXT NOT NULL,
    before_state_json TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    comparison_json TEXT NOT NULL,
    stale_reason TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS settings_versions (
    id TEXT PRIMARY KEY,
    settings_hash TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    description TEXT NOT NULL,
    provider_profile TEXT NOT NULL,
    allowed_connectors_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    memory_namespace TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_profile TEXT NOT NULL,
    provider_name TEXT,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    parent_agent_run_id TEXT,
    correlation_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    from_agent_run_id TEXT,
    to_agent_id TEXT NOT NULL,
    to_agent_role TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS execution_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    result_json TEXT,
    error_text TEXT,
    correlation_id TEXT,
    FOREIGN KEY(job_id) REFERENCES execution_jobs(id)
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider_name TEXT PRIMARY KEY,
    healthy INTEGER NOT NULL,
    circuit_open INTEGER NOT NULL,
    last_error TEXT,
    metadata_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_health (
    connector_name TEXT PRIMARY KEY,
    available INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_created_at ON proposals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_history_started_at ON action_history(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_connector_runs_created_at ON connector_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_status ON execution_jobs(status, queued_at ASC);
CREATE INDEX IF NOT EXISTS idx_audit_entries_created_at ON audit_entries(created_at ASC);
CREATE INDEX IF NOT EXISTS idx_proposal_snapshots_proposal ON proposal_snapshots(proposal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs(run_id, started_at ASC);
CREATE INDEX IF NOT EXISTS idx_handoffs_run_id ON handoffs(run_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_execution_attempts_job_id ON execution_attempts(job_id, started_at DESC);
"""


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        ensure_parent_dir(self.db_path)

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            self._apply_migrations(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with self.connection() as conn:
            conn.execute(query, params)

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(query, params).fetchone()

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(query, params).fetchall()

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        migrations = {
            "proposals": {
                "created_by_agent_id": "TEXT",
                "created_by_agent_role": "TEXT",
                "reviewed_by_agent_id": "TEXT",
                "reviewed_by_agent_role": "TEXT",
                "correlation_id": "TEXT",
                "data_classification": "TEXT NOT NULL DEFAULT 'external-ok'",
                "snapshot_hash": "TEXT",
                "stale_reason": "TEXT",
            },
            "approvals": {
                "snapshot_hash": "TEXT",
                "action_hash": "TEXT",
                "policy_hash": "TEXT",
                "settings_hash": "TEXT",
                "resource_hash": "TEXT",
                "correlation_id": "TEXT",
            },
            "action_history": {
                "correlation_id": "TEXT",
            },
            "execution_jobs": {
                "lease_expires_at": "TEXT",
                "last_heartbeat_at": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "correlation_id": "TEXT",
                "approval_id": "TEXT",
                "dead_letter_reason": "TEXT",
            },
        }
        for table_name, columns in migrations.items():
            existing = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
