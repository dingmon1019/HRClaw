from __future__ import annotations

from app.audit.service import AuditService
from app.runtime.planner import RuntimePlanner
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
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
    ):
        self.planner = planner
        self.queue_service = queue_service
        self.proposal_service = proposal_service
        self.audit_service = audit_service
        self.agent_service = agent_service

    def run_agent(self, request: AgentRunRequest):
        return self.planner.run(request)

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
        self.agent_service.update_nodes_for_proposal(
            proposal_id,
            role=AgentRole.EXECUTOR,
            status="queued",
            details={
                "approval_id": approval.id,
                "job_id": job.id,
                "manifest_hash": approval.manifest_hash,
            },
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
        self.agent_service.update_nodes_for_proposal(
            proposal_id,
            role=AgentRole.EXECUTOR,
            status="blocked",
            details={
                "rejected_by": decision.actor,
                "rejection_reason": decision.reason,
            },
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
