from __future__ import annotations

from app.agents.service import AgentService
from app.audit.service import AuditService
from app.config.settings import AppSettings
from app.runtime.executor import ExecutionDispatcher
from app.schemas.actions import ExecutionJobStatus, ProposalStatus
from app.schemas.agents import AgentRole
from app.services.data_governance_service import DataGovernanceService
from app.services.execution_queue_service import ExecutionQueueService
from app.services.proposal_service import ProposalService


class ExecutionWorker:
    def __init__(
        self,
        worker_id: str,
        base_settings: AppSettings,
        queue_service: ExecutionQueueService,
        proposal_service: ProposalService,
        dispatcher: ExecutionDispatcher,
        audit_service: AuditService,
        agent_service: AgentService,
        data_governance_service: DataGovernanceService,
    ):
        self.worker_id = worker_id
        self.base_settings = base_settings
        self.queue_service = queue_service
        self.proposal_service = proposal_service
        self.dispatcher = dispatcher
        self.audit_service = audit_service
        self.agent_service = agent_service
        self.data_governance_service = data_governance_service

    def run_once(self) -> dict | None:
        claimed = self.queue_service.claim_next_job(
            worker_id=self.worker_id,
            lease_seconds=self.base_settings.worker_lease_seconds,
            max_attempts=self.base_settings.worker_max_attempts,
        )
        if claimed is None:
            return None

        job, attempt = claimed
        proposal = self.proposal_service.get(job.proposal_id)
        executor_agent = self.agent_service.get_by_role(AgentRole.EXECUTOR)
        executor_run = self.agent_service.start_run(
            job.run_id,
            executor_agent,
            input_payload=self.data_governance_service.sanitize_for_history(
                {"job_id": job.id, "proposal_id": job.proposal_id},
                object_type="agent_input",
            ),
            provider_profile="local-only",
            correlation_id=job.correlation_id,
        )
        self.proposal_service.set_execution_status(job.proposal_id, ProposalStatus.RUNNING)
        self.audit_service.emit(
            "execution.started",
            {
                "job_id": job.id,
                "attempt_id": attempt.id,
                "proposal_id": job.proposal_id,
                "worker_id": self.worker_id,
                "correlation_id": job.correlation_id,
            },
        )
        try:
            self.queue_service.heartbeat(job.id, self.worker_id, self.base_settings.worker_lease_seconds)
            result = self.dispatcher.execute_approved(
                job.proposal_id,
                approval_id=job.approval_id,
                expected_manifest_hash=job.manifest_hash,
                executor_agent=executor_agent,
            )
            self.queue_service.mark_finished(job.id, ExecutionJobStatus.EXECUTED, result=result)
            self.agent_service.complete_run(
                executor_run.id,
                status="completed",
                output_payload=self.data_governance_service.sanitize_for_history(
                    {"result": result, "job_id": job.id},
                    object_type="agent_output",
                    action_type=proposal.action_type,
                    connector=proposal.connector,
                ),
                provider_name="local-worker",
            )
            self.audit_service.emit(
                "execution.finished",
                {
                    "job_id": job.id,
                    "attempt_id": attempt.id,
                    "proposal_id": job.proposal_id,
                    "worker_id": self.worker_id,
                    "manifest_hash": job.manifest_hash,
                    "correlation_id": job.correlation_id,
                },
            )
            return result
        except ValueError as exc:
            self.queue_service.mark_finished(job.id, ExecutionJobStatus.BLOCKED, error_text=str(exc))
            self.agent_service.complete_run(
                executor_run.id,
                status="blocked",
                output_payload=self.data_governance_service.sanitize_for_history(
                    {"error": str(exc), "job_id": job.id},
                    object_type="agent_output",
                ),
                provider_name="local-worker",
            )
            self.audit_service.emit(
                "execution.blocked",
                {
                    "job_id": job.id,
                    "attempt_id": attempt.id,
                    "proposal_id": job.proposal_id,
                    "error": str(exc),
                    "manifest_hash": job.manifest_hash,
                    "correlation_id": job.correlation_id,
                },
            )
            raise
        except Exception as exc:
            self.queue_service.mark_finished(job.id, ExecutionJobStatus.FAILED, error_text=str(exc))
            self.agent_service.complete_run(
                executor_run.id,
                status="failed",
                output_payload=self.data_governance_service.sanitize_for_history(
                    {"error": str(exc), "job_id": job.id},
                    object_type="agent_output",
                ),
                provider_name="local-worker",
            )
            self.audit_service.emit(
                "execution.failed",
                {
                    "job_id": job.id,
                    "attempt_id": attempt.id,
                    "proposal_id": job.proposal_id,
                    "error": str(exc),
                    "manifest_hash": job.manifest_hash,
                    "correlation_id": job.correlation_id,
                },
            )
            raise
