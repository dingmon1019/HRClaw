from __future__ import annotations

from app.audit.service import AuditService
from app.runtime.planner import RuntimePlanner
from app.runtime.graph_runtime import GraphRuntimeService
from app.schemas.actions import AgentRunResult, ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
from app.schemas.agents import AgentRole
from app.agents.service import AgentService
from app.services.execution_queue_service import ExecutionQueueService
from app.services.proposal_service import ProposalService


class AgentRuntimeService:
    def __init__(
        self,
        planner: RuntimePlanner,
        queue_service: ExecutionQueueService,
        proposal_service: ProposalService,
        audit_service: AuditService,
        agent_service: AgentService,
        graph_runtime: GraphRuntimeService,
    ):
        self.planner = planner
        self.queue_service = queue_service
        self.proposal_service = proposal_service
        self.audit_service = audit_service
        self.agent_service = agent_service
        self.graph_runtime = graph_runtime

    def run_agent(self, request: AgentRunRequest):
        return self.planner.run(request)

    def describe_run(self, run_id: str) -> AgentRunResult | None:
        return self.planner.describe_run(run_id)

    def approve_and_queue(self, proposal_id: str, decision: ApprovalDecisionRequest) -> dict:
        approval = self.proposal_service.approve(proposal_id, decision.actor, decision.reason)
        proposal = self.proposal_service.get(proposal_id)
        job = self.queue_service.enqueue(
            proposal_id,
            proposal.run_id,
            decision.actor,
            approval_id=approval.id,
            manifest_hash=approval.manifest_hash,
            correlation_id=proposal.correlation_id,
        )
        self.proposal_service.set_execution_status(proposal_id, ProposalStatus.QUEUED)
        self.graph_runtime.sync_proposal_lifecycle(
            proposal_id,
            actor=decision.actor,
            reason=decision.reason,
        )
        self.audit_service.emit(
            "proposal.approved",
            {
                "proposal_id": proposal_id,
                "actor": decision.actor,
                "reason": decision.reason,
                "snapshot_hash": approval.snapshot_hash,
                "action_hash": approval.action_hash,
                "policy_hash": approval.policy_hash,
                "settings_hash": approval.settings_hash,
                "resource_hash": approval.resource_hash,
                "manifest_hash": approval.manifest_hash,
                "correlation_id": proposal.correlation_id,
            },
        )
        self.audit_service.emit(
            "proposal.queued",
            {
                "proposal_id": proposal_id,
                "job_id": job.id,
                "queued_by": decision.actor,
                "approval_id": approval.id,
                "manifest_hash": approval.manifest_hash,
                "correlation_id": proposal.correlation_id,
            },
        )
        return {"approval": approval, "job": job, "proposal": self.proposal_service.get(proposal_id)}

    def reject(self, proposal_id: str, decision: ApprovalDecisionRequest):
        record = self.proposal_service.reject(proposal_id, decision.actor, decision.reason)
        self.graph_runtime.sync_proposal_lifecycle(
            proposal_id,
            actor=decision.actor,
            reason=decision.reason,
        )
        self.audit_service.emit(
            "proposal.rejected",
            {
                "proposal_id": proposal_id,
                "actor": decision.actor,
                "reason": decision.reason,
                "correlation_id": self.proposal_service.get(proposal_id).correlation_id,
            },
        )
        return record

    def cancel_execution(self, proposal_id: str, decision: ApprovalDecisionRequest) -> dict:
        proposal = self.proposal_service.get(proposal_id)
        job = self.queue_service.get_by_proposal_id(proposal_id)
        if job is None:
            raise ValueError("Proposal has no execution job to cancel.")
        cancelled = self.queue_service.cancel(job.id, reason=decision.reason)
        self.proposal_service.set_execution_status(proposal_id, ProposalStatus.APPROVED)
        self.graph_runtime.sync_proposal_lifecycle(
            proposal_id,
            actor=decision.actor,
            reason=decision.reason,
        )
        self.audit_service.emit(
            "proposal.cancelled",
            {
                "proposal_id": proposal_id,
                "job_id": cancelled.id,
                "actor": decision.actor,
                "reason": decision.reason,
                "correlation_id": proposal.correlation_id,
            },
        )
        return {"job": cancelled, "proposal": self.proposal_service.get(proposal_id)}

    def retry_execution(self, proposal_id: str, decision: ApprovalDecisionRequest) -> dict:
        proposal = self.proposal_service.get(proposal_id)
        approval = self.proposal_service.latest_approval(proposal_id)
        if approval is None or approval.decision != "approved":
            raise ValueError("Proposal does not have an approved manifest to retry.")
        job = self.queue_service.enqueue(
            proposal_id,
            proposal.run_id,
            decision.actor,
            approval_id=approval.id,
            manifest_hash=approval.manifest_hash,
            correlation_id=proposal.correlation_id,
        )
        self.proposal_service.set_execution_status(proposal_id, ProposalStatus.QUEUED)
        self.graph_runtime.sync_proposal_lifecycle(
            proposal_id,
            actor=decision.actor,
            reason=decision.reason,
        )
        self.audit_service.emit(
            "proposal.requeued",
            {
                "proposal_id": proposal_id,
                "job_id": job.id,
                "actor": decision.actor,
                "reason": decision.reason,
                "approval_id": approval.id,
                "manifest_hash": approval.manifest_hash,
                "correlation_id": proposal.correlation_id,
            },
        )
        return {"job": job, "proposal": self.proposal_service.get(proposal_id)}
