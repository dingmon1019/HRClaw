from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentRole(str, Enum):
    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    EXECUTOR = "executor"
    REPORTER = "reporter"


class AgentDefinition(BaseModel):
    id: str
    name: str
    role: AgentRole
    description: str
    provider_profile: str
    allowed_connectors: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    memory_namespace: str


class AgentRunRecord(BaseModel):
    id: str
    run_id: str
    agent_id: str
    agent_name: str
    role: AgentRole
    status: str
    provider_profile: str
    provider_name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    parent_agent_run_id: str | None = None
    correlation_id: str | None = None
    started_at: str
    completed_at: str | None = None


class HandoffRecord(BaseModel):
    id: str
    run_id: str
    from_agent_run_id: str | None = None
    to_agent_id: str
    to_agent_role: AgentRole
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    correlation_id: str | None = None
    created_at: str
    completed_at: str | None = None


class TaskNodeRecord(BaseModel):
    id: str
    run_id: str
    parent_task_node_id: str | None = None
    agent_id: str | None = None
    agent_run_id: str | None = None
    handoff_id: str | None = None
    role: AgentRole
    node_type: str
    title: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: str
    provider_profile: str | None = None
    provider_name: str | None = None
    correlation_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    created_at: str
    completed_at: str | None = None
