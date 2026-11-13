"""SQLite connection handling and schema.

A new connection per unit of work keeps things thread safe under FastAPI's
thread pool. Transactions use BEGIN IMMEDIATE so a writer takes the lock
up front instead of failing halfway through.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                  TEXT PRIMARY KEY,
    workflow_name       TEXT NOT NULL,
    workflow_version    INTEGER NOT NULL,
    config_fingerprint  TEXT NOT NULL,
    workflow_snapshot   TEXT NOT NULL,   -- config used for this request (JSON)
    idempotency_key     TEXT NOT NULL,
    payload_hash        TEXT NOT NULL,
    payload             TEXT NOT NULL,   -- original input (JSON), never changed
    context             TEXT NOT NULL,   -- input + dependency results (JSON)
    status              TEXT NOT NULL,
    current_stage       TEXT,            -- where to resume from
    decision            TEXT,
    decision_reason     TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    version             INTEGER NOT NULL DEFAULT 0,  -- optimistic lock
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (workflow_name, idempotency_key)
);

CREATE TABLE IF NOT EXISTS state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES requests(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    stage       TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES requests(id),
    event_type  TEXT NOT NULL,
    stage       TEXT,
    details     TEXT NOT NULL,   -- JSON
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_request ON state_history(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
-- serves the review queue: filter by status, oldest first
DROP INDEX IF EXISTS idx_requests_status;
CREATE INDEX IF NOT EXISTS idx_requests_status_created ON requests(status, created_at);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None: we control BEGIN/COMMIT ourselves.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """All statements inside commit together or not at all."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
