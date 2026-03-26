from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.database import Database
from app.core.errors import InvalidStateError, NotFoundError
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.agents import AgentRole, GraphNodeJobRecord


class GraphNodeQueueService:
    def __init__(self, database: Database):
        self.database = database

    def enqueue(
        self,
        *,
        task_node_id: str,
        run_id: str,
        role: AgentRole,
        node_type: str,
        queued_by: str,
        correlation_id: str | None = None,
    ) -> GraphNodeJobRecord:
        queued_at = utcnow_iso()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM graph_node_jobs WHERE task_node_id = ?",
                (task_node_id,),
            ).fetchone()
            if existing and existing["status"] in {"queued", "running"}:
                return self._row_to_record(existing)
            if existing:
                conn.execute(
                    """
                    UPDATE graph_node_jobs
                    SET status = ?, queued_by = ?, queued_at = ?, started_at = NULL, finished_at = NULL,
                        worker_id = NULL, result_json = NULL, error_text = NULL, lease_expires_at = NULL,
                        last_heartbeat_at = NULL, attempt_count = 0, correlation_id = ?, dead_letter_reason = NULL,
                        cancel_requested_at = NULL, cancel_requested_by = NULL, cancel_reason = NULL,
                        role = ?, node_type = ?
                    WHERE task_node_id = ?
                    """,
                    ("queued", queued_by, queued_at, correlation_id, role.value, node_type, task_node_id),
                )
                row = conn.execute("SELECT * FROM graph_node_jobs WHERE task_node_id = ?", (task_node_id,)).fetchone()
                return self._row_to_record(row)

            job_id = new_id("graphjob")
            conn.execute(
                """
                INSERT INTO graph_node_jobs(
                    id, task_node_id, run_id, role, node_type, status, queued_by, queued_at,
                    started_at, finished_at, worker_id, result_json, error_text, lease_expires_at,
                    last_heartbeat_at, attempt_count, correlation_id, dead_letter_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    task_node_id,
                    run_id,
                    role.value,
                    node_type,
                    "queued",
                    queued_by,
                    queued_at,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    correlation_id,
                    None,
                ),
            )
            row = conn.execute("SELECT * FROM graph_node_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(row)

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
        run_id: str | None = None,
    ) -> GraphNodeJobRecord | None:
        now = datetime.now(UTC).replace(microsecond=0)
        now_iso = now.isoformat()
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        run_filter = "AND run_id = ?" if run_id else ""
        params = [now_iso]
        if run_id:
            params.append(run_id)
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                f"""
                SELECT * FROM graph_node_jobs
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                {run_filter}
                ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, queued_at ASC
                """,
                tuple(params),
            ).fetchall()
            for row in candidates:
                attempt_count = int(row["attempt_count"] or 0) + 1
                if attempt_count > max_attempts:
                    updated = conn.execute(
                        """
                        UPDATE graph_node_jobs
                        SET status = ?, finished_at = ?, dead_letter_reason = ?, lease_expires_at = NULL
                        WHERE id = ? AND (
                            status = 'queued'
                            OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                        )
                        """,
                        ("dead_letter", now_iso, "attempt_limit_reached", row["id"], now_iso),
                    )
                    if updated.rowcount == 1:
                        continue
                updated = conn.execute(
                    """
                    UPDATE graph_node_jobs
                    SET status = ?, started_at = ?, worker_id = ?, lease_expires_at = ?,
                        last_heartbeat_at = ?, attempt_count = ?, dead_letter_reason = NULL
                    WHERE id = ? AND (
                        status = 'queued'
                        OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                    )
                    """,
                    ("running", now_iso, worker_id, lease_expires, now_iso, attempt_count, row["id"], now_iso),
                )
                if updated.rowcount != 1:
                    continue
                claimed = conn.execute("SELECT * FROM graph_node_jobs WHERE id = ?", (row["id"],)).fetchone()
                return self._row_to_record(claimed)
        return None

    def has_claimable_job(self, *, run_id: str | None = None) -> bool:
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
        if run_id:
            row = self.database.fetch_one(
                """
                SELECT id FROM graph_node_jobs
                WHERE (
                    status = 'queued'
                    OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ) AND run_id = ?
                ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, queued_at ASC
                LIMIT 1
                """,
                (now_iso, run_id),
            )
        else:
            row = self.database.fetch_one(
                """
                SELECT id FROM graph_node_jobs
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, queued_at ASC
                LIMIT 1
                """,
                (now_iso,),
            )
        return row is not None

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> GraphNodeJobRecord:
        now = datetime.now(UTC).replace(microsecond=0)
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        self.database.execute(
            """
            UPDATE graph_node_jobs
            SET last_heartbeat_at = ?, lease_expires_at = ?
            WHERE id = ? AND worker_id = ? AND status = 'running'
            """,
            (now.isoformat(), lease_expires, job_id, worker_id),
        )
        return self.get(job_id)

    def mark_finished(
        self,
        job_id: str,
        *,
        status: str,
        result: dict | None = None,
        error_text: str | None = None,
    ) -> GraphNodeJobRecord:
        finished_at = utcnow_iso()
        self.database.execute(
            """
            UPDATE graph_node_jobs
            SET status = ?, finished_at = ?, result_json = ?, error_text = ?,
                lease_expires_at = NULL, last_heartbeat_at = ?
            WHERE id = ?
            """,
            (
                status,
                finished_at,
                json_dumps(result) if result is not None else None,
                error_text,
                finished_at,
                job_id,
            ),
        )
        return self.get(job_id)

    def cancel(self, job_id: str, *, reason: str | None = None) -> GraphNodeJobRecord:
        row = self.database.fetch_one("SELECT * FROM graph_node_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFoundError(f"Graph node job {job_id} was not found.")
        if row["status"] not in {"queued", "failed", "blocked", "dead_letter"}:
            raise InvalidStateError(f"Cannot cancel graph node job in state {row['status']}.")
        finished_at = utcnow_iso()
        self.database.execute(
            """
            UPDATE graph_node_jobs
            SET status = ?, finished_at = ?, error_text = ?, lease_expires_at = NULL, last_heartbeat_at = ?
            WHERE id = ?
            """,
            ("cancelled", finished_at, reason, finished_at, job_id),
        )
        return self.get(job_id)

    def request_cancel(self, job_id: str, *, actor: str, reason: str | None = None) -> GraphNodeJobRecord:
        row = self.database.fetch_one("SELECT * FROM graph_node_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFoundError(f"Graph node job {job_id} was not found.")
        if row["status"] != "running":
            raise InvalidStateError(f"Cannot request cancellation for graph node job in state {row['status']}.")
        self.database.execute(
            """
            UPDATE graph_node_jobs
            SET cancel_requested_at = ?, cancel_requested_by = ?, cancel_reason = ?
            WHERE id = ?
            """,
            (utcnow_iso(), actor, reason, job_id),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> GraphNodeJobRecord:
        row = self.database.fetch_one("SELECT * FROM graph_node_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFoundError(f"Graph node job {job_id} was not found.")
        return self._row_to_record(row)

    def get_by_task_node_id(self, task_node_id: str) -> GraphNodeJobRecord | None:
        row = self.database.fetch_one("SELECT * FROM graph_node_jobs WHERE task_node_id = ?", (task_node_id,))
        return self._row_to_record(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[GraphNodeJobRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM graph_node_jobs WHERE run_id = ? ORDER BY queued_at ASC",
            (run_id,),
        )
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row) -> GraphNodeJobRecord:
        return GraphNodeJobRecord(
            id=row["id"],
            task_node_id=row["task_node_id"],
            run_id=row["run_id"],
            role=AgentRole(row["role"]),
            node_type=row["node_type"],
            status=row["status"],
            queued_by=row["queued_by"],
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            worker_id=row["worker_id"],
            result=json_loads(row["result_json"], {}),
            error_text=row["error_text"],
            lease_expires_at=row["lease_expires_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            attempt_count=int(row["attempt_count"] or 0),
            correlation_id=row["correlation_id"],
            dead_letter_reason=row["dead_letter_reason"],
            cancel_requested_at=row["cancel_requested_at"],
            cancel_requested_by=row["cancel_requested_by"],
            cancel_reason=row["cancel_reason"],
        )
