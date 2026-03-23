from __future__ import annotations

from app.audit.service import AuditService
from app.runtime.executor import ExecutionDispatcher
from app.schemas.actions import ExecutionJobStatus, ProposalStatus
from app.services.execution_queue_service import ExecutionQueueService
from app.services.proposal_service import ProposalService


class ExecutionWorker:
    def __init__(
        self,
        worker_id: str,
        queue_service: ExecutionQueueService,
        proposal_service: ProposalService,
        dispatcher: ExecutionDispatcher,
        audit_service: AuditService,
    ):
        self.worker_id = worker_id
        self.queue_service = queue_service
        self.proposal_service = proposal_service
        self.dispatcher = dispatcher
        self.audit_service = audit_service

    def run_once(self) -> dict | None:
        job = self.queue_service.next_job()
        if job is None:
            return None

        self.queue_service.mark_running(job.id, self.worker_id)
        self.proposal_service.set_execution_status(job.proposal_id, ProposalStatus.RUNNING)
        self.audit_service.emit(
            "worker.job_started",
            {"job_id": job.id, "proposal_id": job.proposal_id, "worker_id": self.worker_id},
        )
        try:
            result = self.dispatcher.execute_approved(job.proposal_id)
            self.queue_service.mark_finished(job.id, ExecutionJobStatus.EXECUTED, result=result)
            self.audit_service.emit(
                "worker.job_finished",
                {"job_id": job.id, "proposal_id": job.proposal_id, "worker_id": self.worker_id},
            )
            return result
        except ValueError as exc:
            self.queue_service.mark_finished(job.id, ExecutionJobStatus.BLOCKED, error_text=str(exc))
            self.audit_service.emit(
                "worker.job_blocked",
                {"job_id": job.id, "proposal_id": job.proposal_id, "error": str(exc)},
            )
            raise
        except Exception as exc:
            self.queue_service.mark_finished(job.id, ExecutionJobStatus.FAILED, error_text=str(exc))
            self.audit_service.emit(
                "worker.job_failed",
                {"job_id": job.id, "proposal_id": job.proposal_id, "error": str(exc)},
            )
            raise
