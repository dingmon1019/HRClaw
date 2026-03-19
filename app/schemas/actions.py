from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RuntimeMode(str, Enum):
    SAFE = "safe"
    RELAXED = "relaxed"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentRunRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=4000)
    filesystem_path: str | None = None
    file_content: str | None = None
    http_url: str | None = None
    http_method: str = "GET"
    http_body: str | None = None
    http_headers_text: str | None = None
    task_title: str | None = None
    task_details: str | None = None
    powershell_command: str | None = None
    provider_name: str | None = None
    model_name: str | None = None


class ActionProposal(BaseModel):
    run_id: str
    objective: str
    connector: str
    action_type: str
    title: str
    description: str
    payload: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None
    policy_notes: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    side_effecting: bool = False
    requires_approval: bool = True
    status: ProposalStatus = ProposalStatus.PENDING
    provider_name: str | None = None
    summary_id: str | None = None


class ProposalRecord(ActionProposal):
    id: str
    created_at: str
    updated_at: str


class ApprovalDecisionRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None


class ApprovalRecord(BaseModel):
    id: str
    proposal_id: str
    decision: str
    actor: str
    reason: str | None = None
    created_at: str


class ActionHistoryRecord(BaseModel):
    id: str
    proposal_id: str
    run_id: str
    connector: str
    action_type: str
    status: str
    started_at: str
    completed_at: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error_text: str | None = None


class ConnectorRunRecord(BaseModel):
    id: str
    run_id: str
    connector: str
    operation: str
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error_text: str | None = None
    created_at: str


class SummaryRecord(BaseModel):
    id: str
    run_id: str
    objective: str
    collected: dict[str, Any]
    summary_text: str
    provider_name: str
    created_at: str


class AgentRunResult(BaseModel):
    run_id: str
    summary: SummaryRecord
    proposals: list[ProposalRecord]

