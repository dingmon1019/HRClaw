from __future__ import annotations

import json

from app.agents.service import AgentService
from app.config.settings import AppSettings
from app.connectors.registry import ConnectorRegistry
from app.core.utils import json_dumps, new_id
from app.memory.service import SummaryService
from app.policy.engine import PolicyEngine
from app.schemas.actions import (
    ActionProposal,
    AgentRunRequest,
    AgentRunResult,
    DataClassification,
)
from app.schemas.agents import AgentDefinition, AgentRole
from app.schemas.providers import ProviderRequest
from app.services.data_governance_service import DataGovernanceService
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService
from app.services.provider_service import ProviderService
from app.audit.service import AuditService


class RuntimePlanner:
    def __init__(
        self,
        base_settings: AppSettings,
        connector_registry: ConnectorRegistry,
        provider_service: ProviderService,
        summary_service: SummaryService,
        proposal_service: ProposalService,
        history_service: HistoryService,
        policy_engine: PolicyEngine,
        audit_service: AuditService,
        agent_service: AgentService,
        data_governance_service: DataGovernanceService,
    ):
        self.base_settings = base_settings
        self.connector_registry = connector_registry
        self.provider_service = provider_service
        self.summary_service = summary_service
        self.proposal_service = proposal_service
        self.history_service = history_service
        self.policy_engine = policy_engine
        self.audit_service = audit_service
        self.agent_service = agent_service
        self.data_governance_service = data_governance_service

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        run_id = new_id("run")
        correlation_id = new_id("corr")
        effective_settings = self.provider_service.settings_service.get_effective_settings()

        supervisor = self.agent_service.get_by_role(AgentRole.SUPERVISOR)
        planner = self.agent_service.get_by_role(AgentRole.PLANNER)
        reviewer = self.agent_service.get_by_role(AgentRole.REVIEWER)
        reporter = self.agent_service.get_by_role(AgentRole.REPORTER)
        self.agent_service.assert_capability(supervisor, "decompose-objective")
        self.agent_service.assert_capability(supervisor, "create-handoffs")
        self.agent_service.assert_capability(planner, "summarize-context")
        self.agent_service.assert_capability(planner, "plan-actions")
        self.agent_service.assert_capability(reviewer, "policy-review")
        self.agent_service.assert_capability(reviewer, "approval-gating")
        self.agent_service.assert_capability(reporter, "summarize-plan")

        supervisor_run = self.agent_service.start_run(
            run_id,
            supervisor,
            input_payload={"objective": request.objective, "request": request.model_dump(mode="json")},
            provider_profile=self._provider_profile_for_role(AgentRole.SUPERVISOR, effective_settings),
            correlation_id=correlation_id,
        )
        subtasks = self._decompose(request)
        intent_summary = self._intent_summary(request, subtasks)
        self.agent_service.complete_run(
            supervisor_run.id,
            status="completed",
            output_payload={"intent_summary": intent_summary, "subtasks": subtasks},
        )

        planner_handoff = self.agent_service.create_handoff(
            run_id,
            from_agent_run_id=supervisor_run.id,
            to_agent=planner,
            title="Objective decomposed for planning",
            payload={"intent_summary": intent_summary, "subtasks": subtasks},
            correlation_id=correlation_id,
        )
        planner_run = self.agent_service.start_run(
            run_id,
            planner,
            input_payload={"subtasks": subtasks, "objective": request.objective},
            parent_agent_run_id=supervisor_run.id,
            provider_profile=self._provider_profile_for_role(AgentRole.PLANNER, effective_settings),
            correlation_id=correlation_id,
        )
        collected = self._collect(run_id, request, supervisor, planner, correlation_id)
        planning_classification = self._planning_classification(request, collected)
        summary_text, summary_provider_name = self._summarize(
            request,
            collected,
            self._provider_profile_for_role(AgentRole.PLANNER, effective_settings),
            correlation_id,
            planning_classification,
        )
        summary = self.summary_service.create(
            run_id=run_id,
            objective=request.objective,
            collected=self.data_governance_service.sanitize_for_history(collected),
            summary_text=summary_text,
            provider_name=summary_provider_name,
        )
        raw_proposals = self._build_proposals(
            run_id=run_id,
            request=request,
            summary_id=summary.id,
            collected=collected,
            summary_text=summary.summary_text,
            planning_provider=request.provider_name or self.provider_service.resolve_profile_provider(
                self._provider_profile_for_role(AgentRole.PLANNER, effective_settings)
            ),
            planner_agent=planner,
            correlation_id=correlation_id,
        )
        self.agent_service.complete_handoff(planner_handoff.id)
        self.agent_service.complete_run(
            planner_run.id,
            status="completed",
            provider_name=summary_provider_name,
            output_payload={
                "summary_id": summary.id,
                "collected_keys": list(collected),
                "proposal_titles": [proposal.title for proposal in raw_proposals],
                "proposal_count": len(raw_proposals),
            },
        )

        reviewer_handoff = self.agent_service.create_handoff(
            run_id,
            from_agent_run_id=planner_run.id,
            to_agent=reviewer,
            title="Candidate actions ready for policy and egress review",
            payload={"proposal_count": len(raw_proposals), "summary_id": summary.id},
            correlation_id=correlation_id,
        )
        reviewer_run = self.agent_service.start_run(
            run_id,
            reviewer,
            input_payload={"summary_id": summary.id, "proposal_count": len(raw_proposals)},
            parent_agent_run_id=planner_run.id,
            provider_profile=self._provider_profile_for_role(AgentRole.REVIEWER, effective_settings),
            correlation_id=correlation_id,
        )
        reviewed = [self._review_proposal(proposal, reviewer) for proposal in raw_proposals]
        created = self.proposal_service.create_many(reviewed)
        self.agent_service.complete_handoff(reviewer_handoff.id)
        self.agent_service.complete_run(
            reviewer_run.id,
            status="completed",
            output_payload={
                "proposal_ids": [proposal.id for proposal in created],
                "blocked_count": sum(1 for proposal in created if proposal.status.value == "blocked"),
                "approval_required_count": sum(1 for proposal in created if proposal.requires_approval),
            },
        )

        reporter_handoff = self.agent_service.create_handoff(
            run_id,
            from_agent_run_id=reviewer_run.id,
            to_agent=reporter,
            title="Reviewed plan ready for operator summary",
            payload={"proposal_ids": [proposal.id for proposal in created]},
            correlation_id=correlation_id,
        )
        reporter_run = self.agent_service.start_run(
            run_id,
            reporter,
            input_payload={"summary_id": summary.id, "proposal_count": len(created)},
            parent_agent_run_id=reviewer_run.id,
            provider_profile=self._provider_profile_for_role(AgentRole.REPORTER, effective_settings),
            correlation_id=correlation_id,
        )
        operator_summary, reporter_provider = self._report_plan(
            request,
            created,
            summary.summary_text,
            self._provider_profile_for_role(AgentRole.REPORTER, effective_settings),
            correlation_id,
            planning_classification,
        )
        self.agent_service.complete_handoff(reporter_handoff.id)
        self.agent_service.complete_run(
            reporter_run.id,
            status="completed",
            provider_name=reporter_provider,
            output_payload={
                "operator_summary": operator_summary,
                "proposal_ids": [proposal.id for proposal in created],
            },
        )

        self.audit_service.emit(
            "planning.completed",
            {
                "run_id": run_id,
                "correlation_id": correlation_id,
                "objective": request.objective,
                "proposal_ids": [proposal.id for proposal in created],
                "summary_id": summary.id,
            },
        )
        return AgentRunResult(run_id=run_id, summary=summary, proposals=created)

    def _collect(
        self,
        run_id: str,
        request: AgentRunRequest,
        supervisor_agent: AgentDefinition,
        planner_agent: AgentDefinition,
        correlation_id: str,
    ) -> dict:
        collected: dict = {"objective": request.objective}
        self.agent_service.assert_connector_allowed(supervisor_agent, "task")
        task_snapshot = self.connector_registry.get("task").collect({"limit": 5})
        collected["tasks"] = task_snapshot
        self.history_service.log_connector_run(
            run_id=run_id,
            connector="task",
            operation="collect",
            status="success",
            payload=self.data_governance_service.sanitize_for_history({"limit": 5}),
            agent_id=supervisor_agent.id,
            agent_role=supervisor_agent.role.value,
            correlation_id=correlation_id,
            output=self.data_governance_service.sanitize_for_history(task_snapshot),
        )
        if request.filesystem_path:
            collected["filesystem"] = self._safe_collect(
                run_id,
                planner_agent,
                "filesystem",
                {"path": request.filesystem_path},
                correlation_id,
            )
        if request.http_url and request.http_method.upper() in {"GET", "HEAD"}:
            collected["http"] = self._safe_collect(
                run_id,
                planner_agent,
                "http",
                {"url": request.http_url},
                correlation_id,
            )
        if request.system_action and request.system_path:
            collected["system"] = self._safe_collect(
                run_id,
                planner_agent,
                "system",
                {"path": request.system_path, "action": request.system_action},
                correlation_id,
            )
        return collected

    def _safe_collect(
        self,
        run_id: str,
        agent: AgentDefinition,
        connector_name: str,
        payload: dict,
        correlation_id: str,
    ) -> dict:
        try:
            self.agent_service.assert_connector_allowed(agent, connector_name)
            output = self.connector_registry.get(connector_name).collect(payload)
            self.history_service.log_connector_run(
                run_id=run_id,
                connector=connector_name,
                operation="collect",
                status="success",
                payload=self.data_governance_service.sanitize_for_history(payload),
                agent_id=agent.id,
                agent_role=agent.role.value,
                correlation_id=correlation_id,
                output=self.data_governance_service.sanitize_for_history(output),
            )
            return output
        except Exception as exc:
            self.history_service.log_connector_run(
                run_id=run_id,
                connector=connector_name,
                operation="collect",
                status="failed",
                payload=self.data_governance_service.sanitize_for_history(payload),
                agent_id=agent.id,
                agent_role=agent.role.value,
                correlation_id=correlation_id,
                error_text=str(exc),
            )
            return {"error": str(exc), "input": payload}

    def _summarize(
        self,
        request: AgentRunRequest,
        collected: dict,
        profile: str,
        correlation_id: str,
        classification: DataClassification,
    ) -> tuple[str, str]:
        prompt = (
            "Summarize the collected local-agent context for a Windows operator. "
            "Highlight risks, side effects, egress implications, and approval-ready actions.\n\n"
            f"Objective:\n{request.objective}\n\nCollected:\n{json_dumps(collected)}"
        )
        provider_name = request.provider_name or self.provider_service.resolve_profile_provider(profile)
        try:
            response = self.provider_service.complete(
                ProviderRequest(
                    provider_name=request.provider_name,
                    model_name=request.model_name,
                    profile=profile,
                    prompt=prompt,
                    system_prompt="Produce a concise operator summary for a local-first multi-agent runtime.",
                    data_classification=classification.value,
                    task_type="planning-summary",
                    correlation_id=correlation_id,
                )
            )
            return response.content, response.provider_name
        except Exception:
            fragments = [f"Objective: {request.objective}"]
            if "filesystem" in collected:
                fragments.append("Filesystem context captured.")
            if "http" in collected:
                fragments.append("HTTP context captured.")
            if "system" in collected:
                fragments.append("Bounded system context captured.")
            if collected.get("tasks"):
                fragments.append("Task snapshot captured.")
            fragments.append("All actions remain approval-gated before execution.")
            return " ".join(fragments), (provider_name or "offline-fallback")

    def _build_proposals(
        self,
        run_id: str,
        request: AgentRunRequest,
        summary_id: str,
        collected: dict,
        summary_text: str,
        planning_provider: str | None,
        planner_agent: AgentDefinition,
        correlation_id: str,
    ) -> list[ActionProposal]:
        proposals: list[ActionProposal] = []
        if request.filesystem_path:
            proposals.extend(
                self._filesystem_proposals(
                    run_id,
                    request,
                    summary_id,
                    collected,
                    planning_provider,
                    planner_agent,
                    correlation_id,
                )
            )
        if request.http_url:
            proposals.append(
                self._http_proposal(
                    run_id,
                    request,
                    summary_id,
                    planning_provider,
                    planner_agent,
                    correlation_id,
                )
            )
        if request.task_title:
            self.agent_service.assert_connector_allowed(planner_agent, "task")
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="task",
                    action_type="task.create",
                    title=f"Create local task: {request.task_title}",
                    description="Create a tracked local task in the runtime database.",
                    payload={"title": request.task_title, "details": request.task_details or summary_text},
                    rationale="The operator asked for a concrete follow-up item.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        elif "list task" in request.objective.lower():
            self.agent_service.assert_connector_allowed(planner_agent, "task")
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="task",
                    action_type="task.list",
                    title="List local tasks",
                    description="Read the current local task backlog from SQLite.",
                    payload={"limit": 20},
                    rationale="The objective explicitly asks for task visibility.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        if request.system_action:
            self.agent_service.assert_connector_allowed(planner_agent, "system")
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="system",
                    action_type=request.system_action,
                    title=f"Run bounded system action {request.system_action}",
                    description="Execute a schema-driven read-only system action.",
                    payload={"path": request.system_path},
                    rationale="The operator selected a bounded system action.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        if not proposals:
            fallback_title = request.task_title or request.objective[:80]
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="task",
                    action_type="task.create",
                    title=f"Capture objective as task: {fallback_title}",
                    description="Create a local follow-up task when no executable connector action was inferred.",
                    payload={"title": fallback_title, "details": summary_text},
                    rationale="No direct connector action was inferred, so the runtime stores the objective as a task.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        return proposals

    def _filesystem_proposals(
        self,
        run_id: str,
        request: AgentRunRequest,
        summary_id: str,
        collected: dict,
        planning_provider: str | None,
        planner_agent: AgentDefinition,
        correlation_id: str,
    ) -> list[ActionProposal]:
        path = request.filesystem_path or ""
        lower_objective = request.objective.lower()
        observed_kind = (collected.get("filesystem") or {}).get("kind")
        proposals: list[ActionProposal] = []
        classification = DataClassification.RESTRICTED if request.file_content is not None else DataClassification.LOCAL_ONLY
        self.agent_service.assert_connector_allowed(planner_agent, "filesystem")

        if request.file_content is not None:
            action_type = "filesystem.append_text" if "append" in lower_objective else "filesystem.write_text"
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="filesystem",
                    action_type=action_type,
                    title=f"{'Append to' if action_type.endswith('append_text') else 'Write'} file {path}",
                    description="Create or update a text file within the configured filesystem allowlist.",
                    payload={"path": path, "content": request.file_content},
                    rationale="The request includes explicit file content.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=classification,
                )
            )
            return proposals

        if "delete" in lower_objective:
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="filesystem",
                    action_type="filesystem.delete_path",
                    title=f"Delete path {path}",
                    description="Delete an allowlisted file or directory.",
                    payload={"path": path},
                    rationale="The objective explicitly requests deletion.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        elif "create folder" in lower_objective or "create directory" in lower_objective or "mkdir" in lower_objective:
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="filesystem",
                    action_type="filesystem.make_directory",
                    title=f"Create directory {path}",
                    description="Create a directory inside an allowlisted root.",
                    payload={"path": path},
                    rationale="The objective explicitly requests a directory.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        elif observed_kind == "directory" or path.endswith(("\\", "/")) or "list" in lower_objective:
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="filesystem",
                    action_type="filesystem.list_directory",
                    title=f"List directory {path}",
                    description="List directory entries within the configured filesystem allowlist.",
                    payload={"path": path},
                    rationale="The objective or collected context points to a directory listing operation.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        else:
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="filesystem",
                    action_type="filesystem.read_text",
                    title=f"Read file {path}",
                    description="Read a text file from the configured filesystem allowlist.",
                    payload={"path": path},
                    rationale="The objective points to a read-oriented filesystem action.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            )
        return proposals

    def _http_proposal(
        self,
        run_id: str,
        request: AgentRunRequest,
        summary_id: str,
        planning_provider: str | None,
        planner_agent: AgentDefinition,
        correlation_id: str,
    ) -> ActionProposal:
        method = request.http_method.upper()
        headers = self._parse_headers(request.http_headers_text)
        classification = DataClassification.RESTRICTED if request.http_body else DataClassification.EXTERNAL_OK
        self.agent_service.assert_connector_allowed(planner_agent, "http")
        return ActionProposal(
            run_id=run_id,
            objective=request.objective,
            connector="http",
            action_type=f"http.{method.lower()}",
            title=f"{method} {request.http_url}",
            description="Execute an HTTP request through the allowlisted HTTP connector.",
            payload={"url": request.http_url, "body": request.http_body, "headers": headers},
            rationale="The operator provided an explicit target URL.",
            provider_name=planning_provider,
            summary_id=summary_id,
            created_by_agent_id=planner_agent.id,
            created_by_agent_role=AgentRole.PLANNER.value,
            correlation_id=correlation_id,
            data_classification=classification,
        )

    def _review_proposal(self, proposal: ActionProposal, reviewer_agent: AgentDefinition) -> ActionProposal:
        policy_notes = list(proposal.policy_notes)
        self.agent_service.assert_capability(reviewer_agent, "egress-review")
        if proposal.data_classification == DataClassification.RESTRICTED:
            policy_notes.append("Data classification is restricted. Remote provider egress is blocked by default.")
        elif proposal.data_classification == DataClassification.LOCAL_ONLY:
            policy_notes.append("Data classification is local-only. Keep planning and review on local-capable providers.")
        reviewed = proposal.model_copy(
            update={
                "policy_notes": policy_notes,
                "reviewed_by_agent_id": reviewer_agent.id,
                "reviewed_by_agent_role": AgentRole.REVIEWER.value,
            }
        )
        return self.policy_engine.evaluate(reviewed)

    def _report_plan(
        self,
        request: AgentRunRequest,
        proposals,
        summary_text: str,
        profile: str,
        correlation_id: str,
        classification: DataClassification,
    ) -> tuple[str, str]:
        prompt = (
            "Summarize this local multi-agent plan for the operator. "
            "Explain why each action is proposed, what requires approval, and the most important risks.\n\n"
            f"Objective:\n{request.objective}\n\nSummary:\n{summary_text}\n\n"
            f"Reviewed proposals:\n{json_dumps([proposal.model_dump(mode='json') for proposal in proposals])}"
        )
        provider_name = request.provider_name or self.provider_service.resolve_profile_provider(profile)
        try:
            response = self.provider_service.complete(
                ProviderRequest(
                    provider_name=request.provider_name,
                    model_name=request.model_name,
                    profile=profile,
                    prompt=prompt,
                    system_prompt="You are the reporter agent in a Windows-first local control console.",
                    data_classification=classification.value,
                    task_type="report-plan",
                    correlation_id=correlation_id,
                )
            )
            return response.content, response.provider_name
        except Exception:
            return (
                "Plan ready. Review the proposed steps, inspect high-risk actions, and approve only the bounded actions you want executed.",
                provider_name or "offline-fallback",
            )

    @staticmethod
    def _parse_headers(raw_headers: str | None) -> dict:
        if not raw_headers:
            return {}
        raw_headers = raw_headers.strip()
        try:
            data = json.loads(raw_headers)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            headers: dict[str, str] = {}
            for line in raw_headers.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip()] = value.strip()
            return headers

    @staticmethod
    def _decompose(request: AgentRunRequest) -> list[dict[str, str]]:
        subtasks: list[dict[str, str]] = [{"role": AgentRole.SUPERVISOR.value, "title": "Interpret objective"}]
        if request.filesystem_path:
            subtasks.append({"role": AgentRole.PLANNER.value, "title": f"Inspect filesystem target {request.filesystem_path}"})
        if request.http_url:
            subtasks.append({"role": AgentRole.PLANNER.value, "title": f"Prepare HTTP action for {request.http_url}"})
        if request.system_action:
            subtasks.append({"role": AgentRole.PLANNER.value, "title": f"Prepare bounded system action {request.system_action}"})
        if request.task_title:
            subtasks.append({"role": AgentRole.PLANNER.value, "title": f"Create local task {request.task_title}"})
        subtasks.append({"role": AgentRole.REVIEWER.value, "title": "Evaluate risk, policy, and egress"})
        subtasks.append({"role": AgentRole.REPORTER.value, "title": "Prepare operator-facing plan summary"})
        return subtasks

    @staticmethod
    def _intent_summary(request: AgentRunRequest, subtasks: list[dict[str, str]]) -> str:
        return (
            f"Objective interpreted as {len(subtasks)} explicit steps. "
            f"The runtime will keep execution behind approval and bounded worker execution for: {request.objective}"
        )

    @staticmethod
    def _planning_classification(request: AgentRunRequest, collected: dict) -> DataClassification:
        if request.file_content or request.http_body:
            return DataClassification.RESTRICTED
        if request.filesystem_path or request.system_action or "filesystem" in collected:
            return DataClassification.LOCAL_ONLY
        return DataClassification.EXTERNAL_OK

    @staticmethod
    def _provider_profile_for_role(role: AgentRole, settings) -> str:
        if role == AgentRole.PLANNER:
            return settings.planning_profile
        if role == AgentRole.REPORTER:
            return settings.summary_profile
        if role == AgentRole.REVIEWER:
            return "privacy-preferred"
        if role == AgentRole.EXECUTOR:
            return "local-only"
        return "strong"
