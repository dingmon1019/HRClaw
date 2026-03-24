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
    QUEUED = "queued"
    RUNNING = "running"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALE = "stale"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DataClassification(str, Enum):
    LOCAL_ONLY = "local-only"
    EXTERNAL_OK = "external-ok"
    RESTRICTED = "restricted"


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
    system_action: str | None = None
    system_path: str | None = None
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
    created_by_agent_id: str | None = None
    created_by_agent_role: str | None = None
    reviewed_by_agent_id: str | None = None
    reviewed_by_agent_role: str | None = None
    correlation_id: str | None = None
    data_classification: DataClassification = DataClassification.EXTERNAL_OK
    snapshot_hash: str | None = None
    stale_reason: str | None = None


class ProposalRecord(ActionProposal):
    id: str
    created_at: str
    updated_at: str


class ApprovalDecisionRequest(BaseModel):
    actor: str = "operator"
    reason: str = Field(min_length=5, max_length=500)
    current_password: str | None = None


class ApprovalRecord(BaseModel):
    id: str
    proposal_id: str
    decision: str
    actor: str
    reason: str | None = None
    created_at: str
    snapshot_hash: str | None = None
    action_hash: str | None = None
    policy_hash: str | None = None
    settings_hash: str | None = None
    resource_hash: str | None = None
    manifest_hash: str | None = None
    correlation_id: str | None = None


class ProposalSnapshotRecord(BaseModel):
    id: str
    proposal_id: str
    snapshot_hash: str
    action_hash: str
    policy_hash: str
    settings_hash: str
    resource_hash: str
    manifest_hash: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    before_state: dict[str, Any] = Field(default_factory=dict)
    preview: dict[str, Any] = Field(default_factory=dict)
    comparison_json: dict[str, Any] = Field(default_factory=dict)
    stale_reason: str | None = None
    status: str
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
    provider_name: str | None = None
    manifest_hash: str | None = None
    correlation_id: str | None = None
    execution_bundle_hash: str | None = None
    boundary_mode: str | None = None
    boundary_metadata: dict[str, Any] | None = None


class ConnectorRunRecord(BaseModel):
    id: str
    run_id: str
    connector: str
    operation: str
    status: str
    agent_id: str | None = None
    agent_role: str | None = None
    correlation_id: str | None = None
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


class ExecutionJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    EXECUTED = "executed"
    FAILED = "failed"
    BLOCKED = "blocked"
    DEAD_LETTER = "dead_letter"


class ExecutionJobRecord(BaseModel):
    id: str
    proposal_id: str
    run_id: str
    status: ExecutionJobStatus
    queued_by: str
    queued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    worker_id: str | None = None
    result: dict[str, Any] | None = None
    error_text: str | None = None
    lease_expires_at: str | None = None
    last_heartbeat_at: str | None = None
    attempt_count: int = 0
    correlation_id: str | None = None
    approval_id: str | None = None
    manifest_hash: str | None = None
    execution_bundle_hash: str | None = None
    boundary_mode: str | None = None
    boundary_metadata: dict[str, Any] | None = None


class ExecutionAttemptRecord(BaseModel):
    id: str
    job_id: str
    attempt_number: int
    status: str
    worker_id: str
    started_at: str
    finished_at: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    result: dict[str, Any] | None = None
    error_text: str | None = None
    correlation_id: str | None = None
    execution_bundle_hash: str | None = None
    boundary_mode: str | None = None
    boundary_metadata: dict[str, Any] | None = None


class ExecutionBoundaryMetadata(BaseModel):
    mode: str
    isolation_level: str
    backend: str | None = None
    environment_scrubbed: bool = True
    allowed_environment_keys: list[str] = Field(default_factory=list)
    secrets_access: str = "denied"
    database_access: str = "none"
    filesystem_scope: list[str] = Field(default_factory=list)
    network_scope: list[str] = Field(default_factory=list)
    granted_file_paths: list[str] = Field(default_factory=list)
    granted_http_targets: list[str] = Field(default_factory=list)
    capability_tokens: list[str] = Field(default_factory=list)
    scope_strategy: str = "connector-bounded"
    cwd: str | None = None
    python_executable: str | None = None
    notes: list[str] = Field(default_factory=list)


class ExecutionBundle(BaseModel):
    proposal_id: str
    run_id: str
    connector: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    manifest_hash: str
    approval_id: str
    correlation_id: str | None = None
    allowed_connectors: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    execution_settings: dict[str, Any] = Field(default_factory=dict)
    boundary: ExecutionBoundaryMetadata
