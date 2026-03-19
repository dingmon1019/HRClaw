from __future__ import annotations

from app.audit.service import AuditService
from app.runtime.executor import ExecutionDispatcher
from app.runtime.planner import RuntimePlanner
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest
from app.services.proposal_service import ProposalService


class AgentRuntimeService:
    def __init__(
        self,
        planner: RuntimePlanner,
        executor: ExecutionDispatcher,
        proposal_service: ProposalService,
        audit_service: AuditService,
    ):
        self.planner = planner
        self.executor = executor
        self.proposal_service = proposal_service
        self.audit_service = audit_service

    def run_agent(self, request: AgentRunRequest):
        return self.planner.run(request)

    def approve_and_execute(self, proposal_id: str, decision: ApprovalDecisionRequest) -> dict:
        approval = self.proposal_service.approve(proposal_id, decision.actor, decision.reason)
        self.audit_service.emit(
            "proposal.approved",
            {"proposal_id": proposal_id, "actor": decision.actor, "reason": decision.reason},
        )
        result = self.executor.execute_approved(proposal_id)
        return {"approval": approval, "result": result, "proposal": self.proposal_service.get(proposal_id)}

    def reject(self, proposal_id: str, decision: ApprovalDecisionRequest):
        record = self.proposal_service.reject(proposal_id, decision.actor, decision.reason)
        self.audit_service.emit(
            "proposal.rejected",
            {"proposal_id": proposal_id, "actor": decision.actor, "reason": decision.reason},
        )
        return record

