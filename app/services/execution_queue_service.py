from __future__ import annotations

from app.core.database import Database
from app.core.errors import InvalidStateError, NotFoundError
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import ExecutionJobRecord, ExecutionJobStatus


class ExecutionQueueService:
    def __init__(self, database: Database):
        self.database = database

    def enqueue(self, proposal_id: str, run_id: str, queued_by: str) -> ExecutionJobRecord:
        existing = self.database.fetch_one(
            "SELECT * FROM execution_jobs WHERE proposal_id = ?",
            (proposal_id,),
        )
        if existing and existing["status"] in {"queued", "running"}:
            raise InvalidStateError("Proposal is already queued for execution.")
        if existing:
            self.database.execute(
                """
                UPDATE execution_jobs
                SET status = ?, queued_by = ?, queued_at = ?, started_at = NULL,
                    finished_at = NULL, worker_id = NULL, result_json = NULL, error_text = NULL
                WHERE proposal_id = ?
                """,
                ("queued", queued_by, utcnow_iso(), proposal_id),
            )
            row = self.database.fetch_one("SELECT * FROM execution_jobs WHERE proposal_id = ?", (proposal_id,))
            return self._row_to_record(row)

        job_id = new_id("job")
        queued_at = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO execution_jobs(
                id, proposal_id, run_id, status, queued_by, queued_at,
                started_at, finished_at, worker_id, result_json, error_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, proposal_id, run_id, "queued", queued_by, queued_at, None, None, None, None, None),
        )
        return self.get(job_id)

    def next_job(self) -> ExecutionJobRecord | None:
        row = self.database.fetch_one(
            "SELECT * FROM execution_jobs WHERE status = 'queued' ORDER BY queued_at ASC LIMIT 1"
        )
        return self._row_to_record(row) if row else None

    def mark_running(self, job_id: str, worker_id: str) -> ExecutionJobRecord:
        self.database.execute(
            """
            UPDATE execution_jobs SET status = ?, started_at = ?, worker_id = ?
            WHERE id = ?
            """,
            ("running", utcnow_iso(), worker_id, job_id),
        )
        return self.get(job_id)

    def mark_finished(
        self,
        job_id: str,
        status: ExecutionJobStatus,
        result: dict | None = None,
        error_text: str | None = None,
    ) -> ExecutionJobRecord:
        self.database.execute(
            """
            UPDATE execution_jobs
            SET status = ?, finished_at = ?, result_json = ?, error_text = ?
            WHERE id = ?
            """,
            (status.value, utcnow_iso(), json_dumps(result) if result is not None else None, error_text, job_id),
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
        )
