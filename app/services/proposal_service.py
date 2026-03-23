from __future__ import annotations

from typing import Iterable

from app.core.database import Database
from app.core.errors import InvalidStateError, NotFoundError
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import (
    ActionProposal,
    ApprovalRecord,
    ProposalRecord,
    ProposalStatus,
)
from app.services.data_governance_service import DataGovernanceService
from app.services.proposal_snapshot_service import ProposalSnapshotService


class ProposalService:
    def __init__(
        self,
        database: Database,
        snapshot_service: ProposalSnapshotService,
        data_governance_service: DataGovernanceService,
    ):
        self.database = database
        self.snapshot_service = snapshot_service
        self.data_governance_service = data_governance_service

    def create_many(self, proposals: Iterable[ActionProposal]) -> list[ProposalRecord]:
        return [self.create(proposal) for proposal in proposals]

    def create(self, proposal: ActionProposal) -> ProposalRecord:
        protected_payload = self.data_governance_service.protect_action_payload(
            proposal.payload,
            classification=proposal.data_classification,
            purpose=f"proposal:{proposal.action_type}",
            action_type=proposal.action_type,
            connector=proposal.connector,
        )
        proposal = proposal.model_copy(update={"payload": protected_payload})
        proposal_id = new_id("proposal")
        now = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO proposals(
                id, run_id, objective, connector, action_type, title, description,
                payload_json, rationale, policy_notes_json, risk_level, side_effecting,
                requires_approval, status, provider_name, summary_id, created_by_agent_id,
                created_by_agent_role, reviewed_by_agent_id, reviewed_by_agent_role,
                correlation_id, data_classification, snapshot_hash, stale_reason, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                proposal.run_id,
                proposal.objective,
                proposal.connector,
                proposal.action_type,
                proposal.title,
                proposal.description,
                json_dumps(proposal.payload),
                proposal.rationale,
                json_dumps(proposal.policy_notes),
                proposal.risk_level.value,
                int(proposal.side_effecting),
                int(proposal.requires_approval),
                proposal.status.value,
                proposal.provider_name,
                proposal.summary_id,
                proposal.created_by_agent_id,
                proposal.created_by_agent_role,
                proposal.reviewed_by_agent_id,
                proposal.reviewed_by_agent_role,
                proposal.correlation_id,
                proposal.data_classification.value,
                proposal.snapshot_hash,
                proposal.stale_reason,
                now,
                now,
            ),
        )
        created = self.get(proposal_id)
        snapshot = self.snapshot_service.capture(created, status="created")
        self.database.execute(
            "UPDATE proposals SET snapshot_hash = ?, updated_at = ? WHERE id = ?",
            (snapshot.snapshot_hash, utcnow_iso(), proposal_id),
        )
        return self.get(proposal_id)

    def list(self, status: str | None = None) -> list[ProposalRecord]:
        if status:
            rows = self.database.fetch_all(
                "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            rows = self.database.fetch_all("SELECT * FROM proposals ORDER BY created_at DESC")
        return [self._row_to_record(row) for row in rows]

    def get(self, proposal_id: str) -> ProposalRecord:
        row = self.database.fetch_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise NotFoundError(f"Proposal {proposal_id} was not found.")
        return self._row_to_record(row)

    def approve(
        self,
        proposal_id: str,
        actor: str,
        reason: str | None = None,
    ) -> ApprovalRecord:
        proposal = self.get(proposal_id)
        if proposal.status not in {ProposalStatus.PENDING, ProposalStatus.STALE}:
            raise InvalidStateError(f"Cannot approve proposal in state {proposal.status.value}.")
        snapshot = self.snapshot_service.capture(proposal, status="approved")
        self._record_approval(proposal_id, "approved", actor, reason, snapshot)
        self._update_status(proposal_id, ProposalStatus.APPROVED)
        self.database.execute(
            "UPDATE proposals SET snapshot_hash = ?, stale_reason = NULL, updated_at = ? WHERE id = ?",
            (snapshot.snapshot_hash, utcnow_iso(), proposal_id),
        )
        return self.list_approvals(proposal_id)[-1]

    def reject(self, proposal_id: str, actor: str, reason: str | None = None) -> ApprovalRecord:
        proposal = self.get(proposal_id)
        if proposal.status not in {ProposalStatus.PENDING, ProposalStatus.STALE}:
            raise InvalidStateError(f"Cannot reject proposal in state {proposal.status.value}.")
        self._record_approval(proposal_id, "rejected", actor, reason, None)
        self._update_status(proposal_id, ProposalStatus.REJECTED)
        return self.list_approvals(proposal_id)[-1]

    def set_execution_status(self, proposal_id: str, status: ProposalStatus, reason: str | None = None) -> None:
        if status not in {
            ProposalStatus.QUEUED,
            ProposalStatus.RUNNING,
            ProposalStatus.EXECUTED,
            ProposalStatus.FAILED,
            ProposalStatus.BLOCKED,
            ProposalStatus.STALE,
        }:
            raise InvalidStateError(f"Unsupported execution status transition: {status.value}.")
        self.database.execute(
            "UPDATE proposals SET status = ?, stale_reason = ?, updated_at = ? WHERE id = ?",
            (status.value, reason, utcnow_iso(), proposal_id),
        )

    def list_approvals(self, proposal_id: str) -> list[ApprovalRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM approvals WHERE proposal_id = ? ORDER BY created_at ASC",
            (proposal_id,),
        )
        return [
            ApprovalRecord(
                id=row["id"],
                proposal_id=row["proposal_id"],
                decision=row["decision"],
                actor=row["actor"],
                reason=row["reason"],
                created_at=row["created_at"],
                snapshot_hash=row["snapshot_hash"],
                action_hash=row["action_hash"],
                policy_hash=row["policy_hash"],
                settings_hash=row["settings_hash"],
                resource_hash=row["resource_hash"],
                manifest_hash=row["manifest_hash"],
                correlation_id=row["correlation_id"],
            )
            for row in rows
        ]

    def latest_approval(self, proposal_id: str) -> ApprovalRecord | None:
        approvals = self.list_approvals(proposal_id)
        return approvals[-1] if approvals else None

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        row = self.database.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            raise NotFoundError(f"Approval {approval_id} was not found.")
        return ApprovalRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            decision=row["decision"],
            actor=row["actor"],
            reason=row["reason"],
            created_at=row["created_at"],
            snapshot_hash=row["snapshot_hash"],
            action_hash=row["action_hash"],
            policy_hash=row["policy_hash"],
            settings_hash=row["settings_hash"],
            resource_hash=row["resource_hash"],
            manifest_hash=row["manifest_hash"],
            correlation_id=row["correlation_id"],
        )

    def _record_approval(
        self,
        proposal_id: str,
        decision: str,
        actor: str,
        reason: str | None,
        snapshot,
    ) -> None:
        self.database.execute(
            """
            INSERT INTO approvals(
                id, proposal_id, decision, actor, reason, snapshot_hash, action_hash,
                policy_hash, settings_hash, resource_hash, manifest_hash, correlation_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("approval"),
                proposal_id,
                decision,
                actor,
                reason,
                snapshot.snapshot_hash if snapshot else None,
                snapshot.action_hash if snapshot else None,
                snapshot.policy_hash if snapshot else None,
                snapshot.settings_hash if snapshot else None,
                snapshot.resource_hash if snapshot else None,
                snapshot.manifest_hash if snapshot else None,
                self.get(proposal_id).correlation_id,
                utcnow_iso(),
            ),
        )

    def _update_status(self, proposal_id: str, status: ProposalStatus) -> None:
        self.database.execute(
            "UPDATE proposals SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, utcnow_iso(), proposal_id),
        )

    @staticmethod
    def _row_to_record(row) -> ProposalRecord:
        return ProposalRecord(
            id=row["id"],
            run_id=row["run_id"],
            objective=row["objective"],
            connector=row["connector"],
            action_type=row["action_type"],
            title=row["title"],
            description=row["description"],
            payload=json_loads(row["payload_json"], {}),
            rationale=row["rationale"],
            policy_notes=json_loads(row["policy_notes_json"], []),
            risk_level=row["risk_level"],
            side_effecting=bool(row["side_effecting"]),
            requires_approval=bool(row["requires_approval"]),
            status=row["status"],
            provider_name=row["provider_name"],
            summary_id=row["summary_id"],
            created_by_agent_id=row["created_by_agent_id"],
            created_by_agent_role=row["created_by_agent_role"],
            reviewed_by_agent_id=row["reviewed_by_agent_id"],
            reviewed_by_agent_role=row["reviewed_by_agent_role"],
            correlation_id=row["correlation_id"],
            data_classification=row["data_classification"],
            snapshot_hash=row["snapshot_hash"],
            stale_reason=row["stale_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
