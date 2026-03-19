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


class ProposalService:
    def __init__(self, database: Database):
        self.database = database

    def create_many(self, proposals: Iterable[ActionProposal]) -> list[ProposalRecord]:
        created: list[ProposalRecord] = []
        for proposal in proposals:
            created.append(self.create(proposal))
        return created

    def create(self, proposal: ActionProposal) -> ProposalRecord:
        proposal_id = new_id("proposal")
        now = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO proposals(
                id, run_id, objective, connector, action_type, title, description,
                payload_json, rationale, policy_notes_json, risk_level, side_effecting,
                requires_approval, status, provider_name, summary_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now,
                now,
            ),
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

    def approve(self, proposal_id: str, actor: str, reason: str | None = None) -> ApprovalRecord:
        proposal = self.get(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise InvalidStateError(f"Cannot approve proposal in state {proposal.status.value}.")
        self._record_approval(proposal_id, "approved", actor, reason)
        self._update_status(proposal_id, ProposalStatus.APPROVED)
        return self.list_approvals(proposal_id)[-1]

    def reject(self, proposal_id: str, actor: str, reason: str | None = None) -> ApprovalRecord:
        proposal = self.get(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise InvalidStateError(f"Cannot reject proposal in state {proposal.status.value}.")
        self._record_approval(proposal_id, "rejected", actor, reason)
        self._update_status(proposal_id, ProposalStatus.REJECTED)
        return self.list_approvals(proposal_id)[-1]

    def set_execution_status(self, proposal_id: str, status: ProposalStatus) -> None:
        if status not in {ProposalStatus.EXECUTED, ProposalStatus.FAILED, ProposalStatus.BLOCKED}:
            raise InvalidStateError(f"Unsupported execution status transition: {status.value}.")
        self._update_status(proposal_id, status)

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
            )
            for row in rows
        ]

    def _record_approval(
        self,
        proposal_id: str,
        decision: str,
        actor: str,
        reason: str | None,
    ) -> None:
        self.database.execute(
            """
            INSERT INTO approvals(id, proposal_id, decision, actor, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("approval"), proposal_id, decision, actor, reason, utcnow_iso()),
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

