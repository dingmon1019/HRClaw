from __future__ import annotations

from app.audit.service import AuditService
from app.connectors.registry import ConnectorRegistry
from app.policy.engine import PolicyEngine
from app.schemas.actions import ProposalStatus
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService


class ExecutionDispatcher:
    def __init__(
        self,
        connector_registry: ConnectorRegistry,
        proposal_service: ProposalService,
        history_service: HistoryService,
        policy_engine: PolicyEngine,
        audit_service: AuditService,
    ):
        self.connector_registry = connector_registry
        self.proposal_service = proposal_service
        self.history_service = history_service
        self.policy_engine = policy_engine
        self.audit_service = audit_service

    def execute_approved(self, proposal_id: str) -> dict:
        proposal = self.proposal_service.get(proposal_id)
        history_id = self.history_service.log_action_start(
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            connector=proposal.connector,
            action_type=proposal.action_type,
            payload=proposal.payload,
        )
        try:
            self.policy_engine.validate_execution(proposal)
            result = self.connector_registry.get(proposal.connector).execute(
                proposal.action_type,
                proposal.payload,
            )
            self.history_service.log_action_end(history_id, "executed", output=result)
            self.proposal_service.set_execution_status(proposal.id, ProposalStatus.EXECUTED)
            self.audit_service.emit(
                "proposal.executed",
                {"proposal_id": proposal.id, "action_type": proposal.action_type, "result": result},
            )
            return result
        except ValueError as exc:
            self.history_service.log_action_end(history_id, "blocked", error_text=str(exc))
            self.proposal_service.set_execution_status(proposal.id, ProposalStatus.BLOCKED)
            self.audit_service.emit(
                "proposal.blocked",
                {"proposal_id": proposal.id, "action_type": proposal.action_type, "error": str(exc)},
            )
            raise
        except Exception as exc:
            self.history_service.log_action_end(history_id, "failed", error_text=str(exc))
            self.proposal_service.set_execution_status(proposal.id, ProposalStatus.FAILED)
            self.audit_service.emit(
                "proposal.failed",
                {"proposal_id": proposal.id, "action_type": proposal.action_type, "error": str(exc)},
            )
            raise

