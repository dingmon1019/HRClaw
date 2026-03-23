from __future__ import annotations

import json

from app.audit.service import AuditService
from app.config.settings import AppSettings
from app.connectors.registry import ConnectorRegistry
from app.core.utils import json_dumps, new_id
from app.memory.service import SummaryService
from app.policy.engine import PolicyEngine
from app.schemas.actions import ActionProposal, AgentRunRequest, AgentRunResult
from app.schemas.providers import ProviderRequest
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService
from app.services.provider_service import ProviderService


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
    ):
        self.base_settings = base_settings
        self.connector_registry = connector_registry
        self.provider_service = provider_service
        self.summary_service = summary_service
        self.proposal_service = proposal_service
        self.history_service = history_service
        self.policy_engine = policy_engine
        self.audit_service = audit_service

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        run_id = new_id("run")
        collected = self._collect(run_id, request)
        effective_settings = self.provider_service.settings_service.get_effective_settings()
        summary_text, summary_provider_name = self._summarize(request, collected, effective_settings.summary_profile)
        summary = self.summary_service.create(
            run_id=run_id,
            objective=request.objective,
            collected=collected,
            summary_text=summary_text,
            provider_name=summary_provider_name,
        )
        proposals = self._build_proposals(
            run_id,
            request,
            summary.id,
            collected,
            summary.summary_text,
            effective_settings.planning_profile,
        )
        created = self.proposal_service.create_many(proposals)
        self.audit_service.emit(
            "planning.completed",
            {
                "run_id": run_id,
                "objective": request.objective,
                "proposal_ids": [proposal.id for proposal in created],
                "summary_id": summary.id,
            },
        )
        return AgentRunResult(run_id=run_id, summary=summary, proposals=created)

    def _collect(self, run_id: str, request: AgentRunRequest) -> dict:
        collected: dict = {"objective": request.objective}
        task_snapshot = self.connector_registry.get("task").collect({"limit": 5})
        collected["tasks"] = task_snapshot
        self.history_service.log_connector_run(
            run_id=run_id,
            connector="task",
            operation="collect",
            status="success",
            payload={"limit": 5},
            output=task_snapshot,
        )
        if request.filesystem_path:
            collected["filesystem"] = self._safe_collect(run_id, "filesystem", {"path": request.filesystem_path})
        if request.http_url and request.http_method.upper() in {"GET", "HEAD"}:
            collected["http"] = self._safe_collect(run_id, "http", {"url": request.http_url})
        return collected

    def _safe_collect(self, run_id: str, connector_name: str, payload: dict) -> dict:
        try:
            output = self.connector_registry.get(connector_name).collect(payload)
            self.history_service.log_connector_run(
                run_id=run_id,
                connector=connector_name,
                operation="collect",
                status="success",
                payload=payload,
                output=output,
            )
            return output
        except Exception as exc:
            self.history_service.log_connector_run(
                run_id=run_id,
                connector=connector_name,
                operation="collect",
                status="failed",
                payload=payload,
                error_text=str(exc),
            )
            return {"error": str(exc), "input": payload}

    def _summarize(self, request: AgentRunRequest, collected: dict, profile: str) -> tuple[str, str]:
        prompt = (
            "Summarize the collected local-agent context for a Windows operator. "
            "Highlight risks, likely side effects, and the next approval-ready actions.\n\n"
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
                    system_prompt="Produce a concise operator summary for a local-first agent runtime.",
                )
            )
            return response.content, response.provider_name
        except Exception:
            fragments = [f"Objective: {request.objective}"]
            if "filesystem" in collected:
                fragments.append("Filesystem context captured.")
            if "http" in collected:
                fragments.append("HTTP context captured.")
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
        planning_profile: str,
    ) -> list[ActionProposal]:
        proposals: list[ActionProposal] = []
        planning_provider = request.provider_name or self.provider_service.resolve_profile_provider(planning_profile)
        if request.filesystem_path:
            proposals.extend(self._filesystem_proposals(run_id, request, summary_id, collected, planning_provider))
        if request.http_url:
            proposals.append(self._http_proposal(run_id, request, summary_id, planning_provider))
        if request.task_title:
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="task",
                    action_type="task.create",
                    title=f"Create local task: {request.task_title}",
                    description="Create a tracked local task in the runtime database.",
                    payload={"title": request.task_title, "details": request.task_details or summary_text},
                    rationale="A local task is an explicit, auditable follow-up artifact.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                )
            )
        elif "list task" in request.objective.lower():
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
                )
            )
        if request.system_action:
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
                )
            )
        return [self.policy_engine.evaluate(proposal) for proposal in proposals]

    def _filesystem_proposals(
        self,
        run_id: str,
        request: AgentRunRequest,
        summary_id: str,
        collected: dict,
        planning_provider: str | None,
    ) -> list[ActionProposal]:
        path = request.filesystem_path or ""
        lower_objective = request.objective.lower()
        observed_kind = (collected.get("filesystem") or {}).get("kind")
        proposals: list[ActionProposal] = []

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
                )
            )
        return proposals

    def _http_proposal(
        self,
        run_id: str,
        request: AgentRunRequest,
        summary_id: str,
        planning_provider: str | None,
    ) -> ActionProposal:
        method = request.http_method.upper()
        headers = self._parse_headers(request.http_headers_text)
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
