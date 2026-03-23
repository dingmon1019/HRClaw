from __future__ import annotations

from app.agents.service import AgentService
from app.audit.service import AuditService
from app.connectors.registry import ConnectorRegistry
from app.policy.engine import PolicyEngine
from app.schemas.actions import ProposalStatus
from app.schemas.agents import AgentDefinition
from app.services.data_governance_service import DataGovernanceService
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService
from app.services.proposal_snapshot_service import ProposalSnapshotService


class ExecutionDispatcher:
    def __init__(
        self,
        connector_registry: ConnectorRegistry,
        proposal_service: ProposalService,
        history_service: HistoryService,
        policy_engine: PolicyEngine,
        audit_service: AuditService,
        snapshot_service: ProposalSnapshotService,
        agent_service: AgentService,
        data_governance_service: DataGovernanceService,
    ):
        self.connector_registry = connector_registry
        self.proposal_service = proposal_service
        self.history_service = history_service
        self.policy_engine = policy_engine
        self.audit_service = audit_service
        self.snapshot_service = snapshot_service
        self.agent_service = agent_service
        self.data_governance_service = data_governance_service

    def execute_approved(
        self,
        proposal_id: str,
        *,
        approval_id: str | None,
        expected_manifest_hash: str | None = None,
        executor_agent: AgentDefinition | None = None,
    ) -> dict:
        proposal = self.proposal_service.get(proposal_id)
        if executor_agent is not None:
            self.agent_service.assert_capability(executor_agent, "execute-approved-action")
            self.agent_service.assert_connector_allowed(executor_agent, proposal.connector)
        if not approval_id:
            raise ValueError("Execution job is missing an approval binding.")
        approval = self.proposal_service.get_approval(approval_id)
        if approval.proposal_id != proposal.id:
            raise ValueError("Execution job approval binding does not match the queued proposal.")
        if expected_manifest_hash and approval.manifest_hash != expected_manifest_hash:
            raise ValueError("Execution job manifest binding does not match the approved manifest.")
        runtime_payload = self.data_governance_service.materialize_action_payload(proposal.payload)
        history_id = self.history_service.log_action_start(
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            connector=proposal.connector,
            action_type=proposal.action_type,
            payload=self.data_governance_service.sanitize_for_history(
                proposal.payload,
                action_type=proposal.action_type,
                connector=proposal.connector,
            ),
            provider_name=proposal.provider_name,
            manifest_hash=approval.manifest_hash,
            correlation_id=proposal.correlation_id,
        )
        try:
            self.snapshot_service.verify_approval_or_raise(proposal, approval)
            self.policy_engine.validate_execution(proposal)
            result = self.connector_registry.get(proposal.connector).execute(
                proposal.action_type,
                runtime_payload,
            )
            safe_result = self.data_governance_service.sanitize_for_history(
                result,
                action_type=proposal.action_type,
                connector=proposal.connector,
            )
            self.history_service.log_action_end(history_id, "executed", output=safe_result)
            self.proposal_service.set_execution_status(proposal.id, ProposalStatus.EXECUTED)
            self.audit_service.emit(
                "proposal.executed",
                {
                    "proposal_id": proposal.id,
                    "action_type": proposal.action_type,
                    "result": safe_result,
                    "approval_id": approval.id,
                    "manifest_hash": approval.manifest_hash,
                    "correlation_id": proposal.correlation_id,
                },
            )
            return result
        except ValueError as exc:
            self.history_service.log_action_end(history_id, "blocked", error_text=str(exc))
            status = ProposalStatus.STALE if "Snapshot drift detected" in str(exc) else ProposalStatus.BLOCKED
            self.proposal_service.set_execution_status(proposal.id, status, reason=str(exc))
            self.audit_service.emit(
                "proposal.blocked",
                {
                    "proposal_id": proposal.id,
                    "action_type": proposal.action_type,
                    "error": str(exc),
                    "approval_id": approval_id,
                    "manifest_hash": approval.manifest_hash,
                    "correlation_id": proposal.correlation_id,
                },
            )
            raise
        except Exception as exc:
            self.history_service.log_action_end(history_id, "failed", error_text=str(exc))
            self.proposal_service.set_execution_status(proposal.id, ProposalStatus.FAILED)
            self.audit_service.emit(
                "proposal.failed",
                {
                    "proposal_id": proposal.id,
                    "action_type": proposal.action_type,
                    "error": str(exc),
                    "approval_id": approval_id,
                    "manifest_hash": approval.manifest_hash,
                    "correlation_id": proposal.correlation_id,
                },
            )
            raise
