from __future__ import annotations

from app.schemas.agents import AgentDefinition, AgentRole


def default_agents() -> list[AgentDefinition]:
    return [
        AgentDefinition(
            id="agent_supervisor",
            name="Supervisor Agent",
            role=AgentRole.SUPERVISOR,
            description="Breaks user objectives into explicit bounded subtasks and tracks completion.",
            provider_profile="strong",
            allowed_connectors=["task"],
            capabilities=["decompose-objective", "track-progress", "create-handoffs"],
            memory_namespace="supervisor",
        ),
        AgentDefinition(
            id="agent_planner",
            name="Planner Agent",
            role=AgentRole.PLANNER,
            description="Turns subtasks into typed candidate actions and execution previews.",
            provider_profile="strong",
            allowed_connectors=["filesystem", "http", "task", "system", "outlook"],
            capabilities=["plan-actions", "select-connectors", "summarize-context"],
            memory_namespace="planner",
        ),
        AgentDefinition(
            id="agent_reviewer",
            name="Reviewer Agent",
            role=AgentRole.REVIEWER,
            description="Checks risk, policy fit, and egress implications before approval.",
            provider_profile="privacy-preferred",
            allowed_connectors=[],
            capabilities=["policy-review", "egress-review", "approval-gating"],
            memory_namespace="reviewer",
        ),
        AgentDefinition(
            id="agent_executor",
            name="Executor Agent",
            role=AgentRole.EXECUTOR,
            description="Executes already approved bounded actions through the worker boundary only.",
            provider_profile="local-only",
            allowed_connectors=["filesystem", "http", "task", "system", "outlook"],
            capabilities=["execute-approved-action", "report-worker-status"],
            memory_namespace="executor",
        ),
        AgentDefinition(
            id="agent_reporter",
            name="Reporter Agent",
            role=AgentRole.REPORTER,
            description="Explains plans, outcomes, failures, and follow-up needs to the operator.",
            provider_profile="fast",
            allowed_connectors=[],
            capabilities=["summarize-plan", "summarize-outcome", "operator-explanation"],
            memory_namespace="reporter",
        ),
    ]
