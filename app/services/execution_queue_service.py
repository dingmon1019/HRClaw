from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.database import Database
from app.core.errors import InvalidStateError, NotFoundError
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import (
    ExecutionAttemptRecord,
    ExecutionJobRecord,
    ExecutionJobStatus,
)


class ExecutionQueueService:
    def __init__(self, database: Database):
        self.database = database

    def enqueue(
        self,
        proposal_id: str,
        run_id: str,
        queued_by: str,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ExecutionJobRecord:
        existing = self.database.fetch_one(
            "SELECT * FROM execution_jobs WHERE proposal_id = ?",
            (proposal_id,),
        )
        queued_at = utcnow_iso()
        if existing and existing["status"] in {"queued", "running"}:
            raise InvalidStateError("Proposal is already queued for execution.")
        if existing:
            self.database.execute(
                """
                UPDATE execution_jobs
                SET status = ?, queued_by = ?, queued_at = ?, started_at = NULL, finished_at = NULL,
                    worker_id = NULL, result_json = NULL, error_text = NULL, lease_expires_at = NULL,
                    last_heartbeat_at = NULL, approval_id = ?, correlation_id = ?, dead_letter_reason = NULL,
                    attempt_count = 0
                WHERE proposal_id = ?
                """,
                ("queued", queued_by, queued_at, approval_id, correlation_id, proposal_id),
            )
            row = self.database.fetch_one("SELECT * FROM execution_jobs WHERE proposal_id = ?", (proposal_id,))
            return self._row_to_record(row)

        job_id = new_id("job")
        self.database.execute(
            """
            INSERT INTO execution_jobs(
                id, proposal_id, run_id, status, queued_by, queued_at, started_at, finished_at,
                worker_id, result_json, error_text, lease_expires_at, last_heartbeat_at,
                attempt_count, correlation_id, approval_id, dead_letter_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                proposal_id,
                run_id,
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
                approval_id,
                None,
            ),
        )
        return self.get(job_id)

    def claim_next_job(
        self,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> tuple[ExecutionJobRecord, ExecutionAttemptRecord] | None:
        now = datetime.now(UTC).replace(microsecond=0)
        now_iso = now.isoformat()
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                """
                SELECT * FROM execution_jobs
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, queued_at ASC
                """,
                (now_iso,),
            ).fetchall()
            for row in candidates:
                attempt_count = int(row["attempt_count"] or 0) + 1
                if attempt_count > max_attempts:
                    update_dead = conn.execute(
                        """
                        UPDATE execution_jobs
                        SET status = ?, finished_at = ?, dead_letter_reason = ?, lease_expires_at = NULL
                        WHERE id = ? AND (
                            status = 'queued'
                            OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                        )
                        """,
                        ("dead_letter", now_iso, "attempt_limit_reached", row["id"], now_iso),
                    )
                    if update_dead.rowcount != 1:
                        continue
                    continue
                claimed_update = conn.execute(
                    """
                    UPDATE execution_jobs
                    SET status = ?, started_at = ?, worker_id = ?, lease_expires_at = ?,
                        last_heartbeat_at = ?, attempt_count = ?, dead_letter_reason = NULL
                    WHERE id = ? AND (
                        status = 'queued'
                        OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                    )
                    """,
                    ("running", now_iso, worker_id, lease_expires, now_iso, attempt_count, row["id"], now_iso),
                )
                if claimed_update.rowcount != 1:
                    continue
                attempt = self._create_attempt(
                    conn=conn,
                    job_id=row["id"],
                    attempt_number=attempt_count,
                    worker_id=worker_id,
                    lease_expires_at=lease_expires,
                    correlation_id=row["correlation_id"],
                )
                claimed = conn.execute("SELECT * FROM execution_jobs WHERE id = ?", (row["id"],)).fetchone()
                return self._row_to_record(claimed), attempt
        return None

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> ExecutionJobRecord:
        now = datetime.now(UTC).replace(microsecond=0)
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        self.database.execute(
            """
            UPDATE execution_jobs
            SET last_heartbeat_at = ?, lease_expires_at = ?
            WHERE id = ? AND worker_id = ? AND status = 'running'
            """,
            (now.isoformat(), lease_expires, job_id, worker_id),
        )
        self.database.execute(
            """
            UPDATE execution_attempts
            SET heartbeat_at = ?, lease_expires_at = ?
            WHERE job_id = ? AND worker_id = ? AND status = 'running'
            """,
            (now.isoformat(), lease_expires, job_id, worker_id),
        )
        return self.get(job_id)

    def mark_finished(
        self,
        job_id: str,
        status: ExecutionJobStatus,
        result: dict | None = None,
        error_text: str | None = None,
    ) -> ExecutionJobRecord:
        finished_at = utcnow_iso()
        self.database.execute(
            """
            UPDATE execution_jobs
            SET status = ?, finished_at = ?, result_json = ?, error_text = ?,
                lease_expires_at = NULL, last_heartbeat_at = ?
            WHERE id = ?
            """,
            (
                status.value,
                finished_at,
                json_dumps(result) if result is not None else None,
                error_text,
                finished_at,
                job_id,
            ),
        )
        self.database.execute(
            """
            UPDATE execution_attempts
            SET status = ?, finished_at = ?, result_json = ?, error_text = ?, heartbeat_at = ?
            WHERE job_id = ? AND status = 'running'
            """,
            (
                status.value,
                finished_at,
                json_dumps(result) if result is not None else None,
                error_text,
                finished_at,
                job_id,
            ),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> ExecutionJobRecord:
        row = self.database.fetch_one("SELECT * FROM execution_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFoundError(f"Execution job {job_id} was not found.")
        return self._row_to_record(row)

    def list_recent(self, limit: int = 50) -> list[ExecutionJobRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM execution_jobs ORDER BY queued_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_record(row) for row in rows]

    def list_attempts(self, job_id: str) -> list[ExecutionAttemptRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM execution_attempts WHERE job_id = ? ORDER BY started_at DESC",
            (job_id,),
        )
        return [self._attempt_row_to_record(row) for row in rows]

    @staticmethod
    def _create_attempt(
        conn,
        job_id: str,
        attempt_number: int,
        worker_id: str,
        lease_expires_at: str,
        correlation_id: str | None,
    ) -> ExecutionAttemptRecord:
        attempt_id = new_id("attempt")
        started_at = utcnow_iso()
        conn.execute(
            """
            INSERT INTO execution_attempts(
                id, job_id, attempt_number, status, worker_id, started_at, finished_at,
                lease_expires_at, heartbeat_at, result_json, error_text, correlation_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                job_id,
                attempt_number,
                "running",
                worker_id,
                started_at,
                None,
                lease_expires_at,
                started_at,
                None,
                None,
                correlation_id,
            ),
        )
        row = conn.execute("SELECT * FROM execution_attempts WHERE id = ?", (attempt_id,)).fetchone()
        return ExecutionQueueService._attempt_row_to_record(row)

    @staticmethod
    def _row_to_record(row) -> ExecutionJobRecord:
        return ExecutionJobRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            run_id=row["run_id"],
            status=row["status"],
            queued_by=row["queued_by"],
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            worker_id=row["worker_id"],
            result=json_loads(row["result_json"], None),
            error_text=row["error_text"],
            lease_expires_at=row["lease_expires_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            attempt_count=int(row["attempt_count"] or 0),
            correlation_id=row["correlation_id"],
            approval_id=row["approval_id"],
        )

    @staticmethod
    def _attempt_row_to_record(row) -> ExecutionAttemptRecord:
        return ExecutionAttemptRecord(
            id=row["id"],
            job_id=row["job_id"],
            attempt_number=int(row["attempt_number"]),
            status=row["status"],
            worker_id=row["worker_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            result=json_loads(row["result_json"], None),
            error_text=row["error_text"],
            correlation_id=row["correlation_id"],
        )
