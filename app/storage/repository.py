"""Reads and writes requests, state history and audit events.

Every change to a request goes through commit_step(), which writes the
request row, the history row and the audit rows in ONE transaction. So a
crash or storage error can never leave a decision without its audit trail,
or an audit trail for a decision that was never saved.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from app.core.errors import ConcurrentModification, DuplicateRequestKey, PersistenceError
from app.storage.db import Database

JSON_COLUMNS = ("workflow_snapshot", "payload", "context")
UPDATABLE_COLUMNS = {
    "status", "current_stage", "context", "decision", "decision_reason", "attempt_count",
}

# Called inside the transaction just before COMMIT. Tests use it to
# simulate a storage failure halfway through a save.
FaultInjector = Callable[[str], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RequestRepository:
    def __init__(self, db: Database, fault_injector: Optional[FaultInjector] = None):
        self._db = db
        self.fault_injector = fault_injector

    # ----------------------------------------------------------------- writes
    def create(self, record: dict, events: list[dict]) -> None:
        now = utc_now()
        row = {**record, "created_at": now, "updated_at": now, "version": 0}
        for col in JSON_COLUMNS:
            row[col] = json.dumps(row[col])
        columns = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        try:
            with self._db.transaction() as conn:
                conn.execute(f"INSERT INTO requests ({columns}) VALUES ({placeholders})", row)
                self._insert_history(conn, record["id"], None, record["status"], record["current_stage"], "request received", now)
                self._insert_events(conn, record["id"], events, now)
                self._inject("create")
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateRequestKey(record["idempotency_key"]) from exc
            raise PersistenceError("could not save new request", details=str(exc)) from exc
        except sqlite3.Error as exc:
            raise PersistenceError("could not save new request", details=str(exc)) from exc

    def commit_step(
        self,
        request_id: str,
        expected_version: int,
        updates: dict,
        events: list[dict],
        transition: Optional[tuple[str, str, Optional[str], str]] = None,
    ) -> int:
        """Atomically update the request, append history and audit rows.

        transition = (from_status, to_status, stage, reason) or None.
        Returns the new version. Raises ConcurrentModification if another
        writer changed the request since it was read.
        """
        unknown = set(updates) - UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"cannot update columns: {unknown}")

        now = utc_now()
        values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in updates.items()}
        set_clause = ", ".join(f"{k} = :{k}" for k in values)
        if set_clause:
            set_clause += ", "
        params = {**values, "id": request_id, "expected": expected_version, "now": now}

        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    f"UPDATE requests SET {set_clause}version = version + 1, updated_at = :now "
                    "WHERE id = :id AND version = :expected",
                    params,
                )
                if cursor.rowcount == 0:
                    raise ConcurrentModification(
                        "request was modified by another process, reload and try again",
                        details={"request_id": request_id},
                    )
                if transition:
                    from_status, to_status, stage, reason = transition
                    self._insert_history(conn, request_id, from_status, to_status, stage, reason, now)
                self._insert_events(conn, request_id, events, now)
                self._inject("commit_step")
        except sqlite3.Error as exc:
            raise PersistenceError("could not save request progress", details=str(exc)) from exc
        return expected_version + 1

    def append_audit(self, request_id: str, events: list[dict]) -> None:
        try:
            with self._db.transaction() as conn:
                self._insert_events(conn, request_id, events, utc_now())
        except sqlite3.Error as exc:
            raise PersistenceError("could not write audit event", details=str(exc)) from exc

    # ------------------------------------------------------------------ reads
    def get(self, request_id: str) -> Optional[dict]:
        with self._db.connection() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._decode(row)

    def find_by_idempotency_key(self, workflow_name: str, key: str) -> Optional[dict]:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE workflow_name = ? AND idempotency_key = ?",
                (workflow_name, key),
            ).fetchone()
        return self._decode(row)

    def history(self, request_id: str) -> list[dict]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT from_status, to_status, stage, reason, created_at FROM state_history "
                "WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def audit(self, request_id: str) -> list[dict]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id, event_type, stage, details, created_at FROM audit_log "
                "WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    def find_stuck(self, updated_before: str) -> list[str]:
        """IN_PROGRESS requests whose last saved progress is older than the cutoff."""
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM requests WHERE status = 'IN_PROGRESS' AND updated_at <= ? ORDER BY updated_at",
                (updated_before,),
            ).fetchall()
        return [r["id"] for r in rows]

    def list_requests(
        self, status: Optional[str], workflow: Optional[str], limit: int, offset: int
    ) -> tuple[list[dict], int]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if workflow:
            where.append("workflow_name = ?")
            params.append(workflow)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._db.connection() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM requests {clause}", params).fetchone()[0]
            rows = conn.execute(
                "SELECT id, workflow_name, workflow_version, status, decision, decision_reason, "
                "current_stage, attempt_count, created_at, updated_at "
                f"FROM requests {clause} ORDER BY created_at, id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(r) for r in rows], total

    def workflow_versions_used(self, workflow_name: str) -> dict[int, set[str]]:
        """Which versions (and content fingerprints) stored requests ran under."""
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT workflow_version, config_fingerprint FROM requests WHERE workflow_name = ?",
                (workflow_name,),
            ).fetchall()
        used: dict[int, set[str]] = {}
        for row in rows:
            used.setdefault(row["workflow_version"], set()).add(row["config_fingerprint"])
        return used

    def count_requests(self) -> int:
        with self._db.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    # ---------------------------------------------------------------- helpers
    def _inject(self, operation: str) -> None:
        if self.fault_injector:
            self.fault_injector(operation)

    @staticmethod
    def _decode(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        for col in JSON_COLUMNS:
            data[col] = json.loads(data[col])
        return data

    @staticmethod
    def _insert_history(conn, request_id, from_status, to_status, stage, reason, now) -> None:
        conn.execute(
            "INSERT INTO state_history (request_id, from_status, to_status, stage, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, from_status, to_status, stage, reason, now),
        )

    @staticmethod
    def _insert_events(conn, request_id: str, events: list[dict], now: str) -> None:
        conn.executemany(
            "INSERT INTO audit_log (request_id, event_type, stage, details, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (request_id, e["event_type"], e.get("stage"), json.dumps(e.get("details", {}), default=str), now)
                for e in events
            ],
        )
