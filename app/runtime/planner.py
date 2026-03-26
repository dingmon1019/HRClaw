from __future__ import annotations

import json

from app.agents.service import AgentService
from app.core.errors import CancellationRequestedError
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
    PlanningStatus,
)
from app.schemas.agents import AgentDefinition, AgentRole
from app.schemas.providers import ProviderRequest
from app.services.data_governance_service import DataGovernanceService
from app.services.agent_workspace_service import AgentWorkspaceService
from app.services.artifact_lineage_service import ArtifactLineageService
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService
from app.services.provider_service import ProviderService
from app.audit.service import AuditService
from app.runtime.graph_scheduler import TaskGraphScheduler


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
        agent_workspace_service: AgentWorkspaceService,
        artifact_lineage_service: ArtifactLineageService,
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
        self.agent_workspace_service = agent_workspace_service
        self.artifact_lineage_service = artifact_lineage_service
        self.graph_runtime = None

    def attach_graph_runtime(self, graph_runtime) -> None:
        self.graph_runtime = graph_runtime

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        run_id = new_id("run")
        correlation_id = new_id("corr")
        effective_settings = self.provider_service.settings_service.get_effective_settings()
        request_payload = request.model_dump(mode="json")

        supervisor = self.agent_service.get_by_role(AgentRole.SUPERVISOR)
        planner = self.agent_service.get_by_role(AgentRole.PLANNER)
        reviewer = self.agent_service.get_by_role(AgentRole.REVIEWER)
        reporter = self.agent_service.get_by_role(AgentRole.REPORTER)
        objective_namespace = f"{supervisor.memory_namespace}:{run_id}:objective"
        self.agent_service.assert_capability(supervisor, "decompose-objective")
        self.agent_service.assert_capability(supervisor, "create-handoffs")
        self.agent_service.assert_capability(planner, "summarize-context")
        self.agent_service.assert_capability(planner, "plan-actions")
        self.agent_service.assert_capability(reviewer, "policy-review")
        self.agent_service.assert_capability(reviewer, "approval-gating")
        self.agent_service.assert_capability(reporter, "summarize-plan")
        if self.graph_runtime is None:
            raise RuntimeError("Graph runtime is not attached.")
        self.graph_runtime.register_run(
            run_id=run_id,
            request_payload=request_payload,
            summary_id=None,
            planner_run_id=None,
            planner_handoff_id=None,
            correlation_id=correlation_id,
            initial_state={
                "admission": "accepted",
                "graph_execution_mode": self.base_settings.graph_execution_mode,
                "planning_completed": False,
            },
        )
        objective_node = self.agent_service.create_task_node(
            run_id,
            role=AgentRole.SUPERVISOR,
            node_type="objective",
            title=request.objective,
            details=self._sanitize_task_node_details(
                {
                    "request": request.model_dump(mode="json"),
                    "memory_namespace": supervisor.memory_namespace,
                    "input_source": "operator-objective",
                    "capabilities": supervisor.capabilities,
                    "allowed_connectors": supervisor.allowed_connectors,
                }
                | self._agent_work_area_details(
                    run_id=run_id,
                        agent=supervisor,
                        context_namespace=objective_namespace,
                        branch_key="objective",
                    )
            ),
            status="ready",
            parent_task_node_id=None,
            branch_key="objective",
            context_namespace=objective_namespace,
            agent=supervisor,
            provider_profile=self._provider_profile_for_role(AgentRole.SUPERVISOR, effective_settings),
            correlation_id=correlation_id,
            depends_on=[],
        )
        self.graph_runtime.advance_run(
            run_id,
            execute_inline=self.base_settings.graph_execution_mode == "inline_compat",
        )
        result = self.describe_run(run_id)
        if result is None:
            raise RuntimeError(f"Graph run {run_id} was not created.")
        return result

    def describe_run(self, run_id: str) -> AgentRunResult | None:
        if self.graph_runtime is None:
            raise RuntimeError("Graph runtime is not attached.")
        graph_context = self.graph_runtime.get_run_context(run_id)
        summary = self.summary_service.get_by_run_id(run_id)
        proposals = [proposal for proposal in self.proposal_service.list() if proposal.run_id == run_id]
        task_nodes = self.agent_service.list_task_nodes(run_id)
        if graph_context is None and summary is None and not proposals and not task_nodes:
            return None
        planning_status = self._planning_status(graph_context, task_nodes, summary, proposals)
        return AgentRunResult(
            run_id=run_id,
            planning_status=planning_status,
            graph_status=(graph_context or {}).get("status", "unknown"),
            message=self._planning_message(planning_status),
            summary=summary,
            proposals=proposals,
        )

    def _planning_status(self, graph_context, task_nodes, summary, proposals) -> PlanningStatus:
        if graph_context and graph_context.get("status") == "failed":
            return PlanningStatus.PLANNING_FAILED
        if graph_context and (
            graph_context.get("state", {}).get("planning_completed") or graph_context.get("status") == "waiting_approval"
        ):
            return PlanningStatus.PLANNING_COMPLETED
        if summary is not None and proposals:
            return PlanningStatus.PLANNING_COMPLETED
        non_executor_nodes = [
            node for node in task_nodes if node.role != AgentRole.EXECUTOR and not (node.proposal_id and node.node_type == "proposal")
        ]
        if any(node.status == "running" for node in non_executor_nodes):
            return PlanningStatus.PLANNING_RUNNING
        if any(node.status == "queued" for node in non_executor_nodes):
            return PlanningStatus.PLANNING_QUEUED
        if non_executor_nodes:
            return PlanningStatus.ACCEPTED
        return PlanningStatus.ACCEPTED

    @staticmethod
    def _planning_message(planning_status: PlanningStatus) -> str:
        messages = {
            PlanningStatus.ACCEPTED: "Run accepted. Planning graph admission is in progress.",
            PlanningStatus.PLANNING_QUEUED: "Run accepted. Planning is queued in the graph runtime.",
            PlanningStatus.PLANNING_RUNNING: "Run accepted. Planning is currently running.",
            PlanningStatus.PLANNING_COMPLETED: "Planning completed. Review the generated proposals.",
            PlanningStatus.PLANNING_FAILED: "Planning failed. Inspect the graph run details for the failing node.",
        }
        return messages[planning_status]

    def execute_objective_node(
        self,
        *,
        node,
        request_payload: dict,
        correlation_id: str,
        effective_settings,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict:
        request = AgentRunRequest.model_validate(request_payload)
        supervisor = self.agent_service.get_by_role(AgentRole.SUPERVISOR)
        planner_agent = self.agent_service.get_by_role(AgentRole.PLANNER)
        self.agent_service.update_task_node(
            node.id,
            status="running",
            details=self._sanitize_task_node_details(
                {
                    "context_namespace": node.context_namespace,
                    "attempt_count": int(node.details.get("attempt_count", 0)) + 1,
                }
            ),
            clear_completion=True,
        )
        supervisor_run = self.agent_service.start_run(
            node.run_id,
            supervisor,
            input_payload=self._sanitize_agent_payload(
                {"objective": request.objective, "request": request_payload},
                object_type="agent_input",
            ),
            provider_profile=self._provider_profile_for_role(AgentRole.SUPERVISOR, effective_settings),
            correlation_id=correlation_id,
        )
        try:
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            subtasks = self._decompose(request)
            intent_summary = self._intent_summary(request, subtasks)
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            graph_context = self.graph_runtime.get_run_context(node.run_id) if self.graph_runtime else None
            planner_handoff_id = graph_context.get("planner_handoff_id") if graph_context else None
            planner_run_id = graph_context.get("planner_run_id") if graph_context else None
            if planner_handoff_id is None or planner_run_id is None:
                planner_handoff = self.agent_service.create_handoff(
                    node.run_id,
                    from_agent_run_id=supervisor_run.id,
                    to_agent=planner_agent,
                    title="Objective decomposed for planning",
                    payload=self._sanitize_handoff_payload({"intent_summary": intent_summary, "subtasks": subtasks}),
                    correlation_id=correlation_id,
                )
                planner_run = self.agent_service.start_run(
                    node.run_id,
                    planner_agent,
                    input_payload=self._sanitize_agent_payload(
                        {"subtasks": subtasks, "objective": request.objective},
                        object_type="agent_input",
                    ),
                    parent_agent_run_id=supervisor_run.id,
                    provider_profile=self._provider_profile_for_role(AgentRole.PLANNER, effective_settings),
                    correlation_id=correlation_id,
                )
                planner_handoff_id = planner_handoff.id
                planner_run_id = planner_run.id
            summary_node, created_nodes = self._ensure_graph_planning_nodes(
                run_id=node.run_id,
                request=request,
                objective_node=node,
                subtasks=subtasks,
                correlation_id=correlation_id,
                settings=effective_settings,
            )
            self.agent_service.complete_run(
                supervisor_run.id,
                status="completed",
                output_payload=self._sanitize_agent_payload(
                    {"intent_summary": intent_summary, "subtasks": subtasks},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="completed",
                details=self._sanitize_task_node_details(
                    {
                        "intent_summary": intent_summary,
                        "subtask_count": len(subtasks),
                        "subtask_titles": [subtask["title"] for subtask in subtasks],
                        "subtask_node_ids": [created.id for created in created_nodes],
                        "summary_node_id": summary_node.id,
                    }
                ),
                agent_run_id=supervisor_run.id,
            )
            return {
                "status": "completed",
                "intent_summary": intent_summary,
                "subtask_count": len(subtasks),
                "subtasks": [{"title": subtask["title"], "role": subtask["role"]} for subtask in subtasks],
                "planner_handoff_id": planner_handoff_id,
                "planner_run_id": planner_run_id,
                "summary_node_id": summary_node.id,
                "planner_graph_ready": True,
            }
        except CancellationRequestedError as exc:
            self.agent_service.complete_run(
                supervisor_run.id,
                status="cancelled",
                output_payload=self._sanitize_agent_payload(
                    {"objective": request.objective, "error": str(exc)},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="cancelled",
                details=self._sanitize_task_node_details(
                    {"objective_cancelled": True, "cancel_mode": "cooperative", "error": str(exc)}
                ),
                agent_run_id=supervisor_run.id,
            )
            raise
        except Exception:
            self.agent_service.complete_run(
                supervisor_run.id,
                status="failed",
                output_payload=self._sanitize_agent_payload({"objective": request.objective}, object_type="agent_output"),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="failed",
                details=self._sanitize_task_node_details({"objective_failed": True}),
                agent_run_id=supervisor_run.id,
            )
            raise

    def execute_summary_node(
        self,
        *,
        node,
        request_payload: dict,
        planner_run_id: str,
        correlation_id: str,
        effective_settings,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict:
        request = AgentRunRequest.model_validate(request_payload)
        supervisor = self.agent_service.get_by_role(AgentRole.SUPERVISOR)
        planner_agent = self.agent_service.get_by_role(AgentRole.PLANNER)
        provider_profile = node.provider_profile or self._provider_profile_for_role(AgentRole.PLANNER, effective_settings)
        self.agent_service.update_task_node(
            node.id,
            status="running",
            details=self._sanitize_task_node_details(
                {
                    "phase": "planning-summary",
                    "attempt_count": int(node.details.get("attempt_count", 0)) + 1,
                }
            ),
            clear_completion=True,
        )
        summary_run = self.agent_service.start_run(
            node.run_id,
            planner_agent,
            input_payload=self._sanitize_agent_payload(
                {"objective": request.objective, "phase": "planning-summary"},
                object_type="agent_input",
            ),
            provider_profile=provider_profile,
            parent_agent_run_id=planner_run_id,
            correlation_id=correlation_id,
        )
        try:
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            collected = self._collect(node.run_id, request, supervisor, planner_agent, correlation_id)
            local_collected_context, provider_collected_context, planning_governance = (
                self.data_governance_service.build_planning_context_views(request_payload, collected)
            )
            planning_classification = DataClassification(planning_governance["local_context_classification"])
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            summary_text, summary_provider_name, summary_routing = self._summarize(
                request,
                local_collected_context,
                provider_collected_context,
                provider_profile,
                correlation_id,
                planning_classification,
                planning_governance,
            )
            summary_lineage = self.data_governance_service.build_derived_lineage(
                source_kind="planning-summary",
                source_classification=planning_classification,
                blocked_sections=planning_governance.get("blocked_sections", []),
                sendable_sections=planning_governance.get("sendable_sections", []),
                reasons=planning_governance.get("reasons", []),
            )
            outbound_summary_text = self.data_governance_service.curate_derived_summary_for_outbound(
                summary_text,
                source_classification=planning_classification,
                lineage=summary_lineage,
            )
            summary = self.summary_service.create(
                run_id=node.run_id,
                objective=request.objective,
                collected=self.data_governance_service.sanitize_for_history(
                    local_collected_context,
                    object_type="summary_payload",
                ),
                summary_text=summary_text,
                provider_name=summary_provider_name,
                data_classification=planning_classification,
                lineage=summary_lineage,
                outbound_summary_text=outbound_summary_text,
            )
            self.agent_service.complete_run(
                summary_run.id,
                status="completed",
                provider_name=summary_provider_name,
                output_payload=self._sanitize_agent_payload(
                    {
                        "summary_id": summary.id,
                        "collected_keys": list(collected),
                        "deferred_evidence_count": len(local_collected_context.get("deferred_evidence", [])),
                    },
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="completed",
                details=self._sanitize_task_node_details(
                    {
                        "summary_id": summary.id,
                        "summary_provider_name": summary_provider_name,
                        "collected_keys": list(collected),
                        "deferred_evidence_count": len(local_collected_context.get("deferred_evidence", [])),
                    }
                ),
                provider_name=summary_provider_name,
                agent_run_id=summary_run.id,
            )
            return {
                "status": "completed",
                "summary_id": summary.id,
                "provider_name": summary_provider_name,
                "summary_provider_routing": summary_routing,
                "collected_keys": list(collected),
                "deferred_evidence_count": len(local_collected_context.get("deferred_evidence", [])),
                "data_classification": planning_classification.value,
            }
        except CancellationRequestedError as exc:
            self.agent_service.complete_run(
                summary_run.id,
                status="cancelled",
                output_payload=self._sanitize_agent_payload(
                    {"phase": "planning-summary", "error": str(exc)},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="cancelled",
                details=self._sanitize_task_node_details(
                    {"summary_generation_cancelled": True, "cancel_mode": "cooperative", "error": str(exc)}
                ),
                agent_run_id=summary_run.id,
            )
            raise
        except Exception:
            self.agent_service.complete_run(
                summary_run.id,
                status="failed",
                output_payload=self._sanitize_agent_payload({"phase": "planning-summary"}, object_type="agent_output"),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="failed",
                details=self._sanitize_task_node_details({"summary_generation_failed": True}),
                agent_run_id=summary_run.id,
            )
            raise

    def _collect(
        self,
        run_id: str,
        request: AgentRunRequest,
        supervisor_agent: AgentDefinition,
        planner_agent: AgentDefinition,
        correlation_id: str,
    ) -> dict:
        collected: dict = {
            "objective": request.objective,
            "planning_collection_mode": "descriptor-only",
        }
        deferred_evidence: list[dict] = []
        if self._task_backlog_requested(request):
            self.agent_service.assert_connector_allowed(supervisor_agent, "task")
            task_descriptor = self._task_planning_descriptor()
            collected["tasks"] = task_descriptor
            self._log_planning_descriptor(
                run_id,
                supervisor_agent,
                "task",
                {"limit": 5},
                task_descriptor,
                correlation_id,
            )
            deferred_evidence.append(
                self._deferred_evidence_entry(
                    connector="task",
                    action_type="task.list",
                    title="Read local task backlog for planning evidence",
                    reason="Planning did not inspect the runtime task backlog before approval.",
                    target={"limit": 20},
                )
            )
        if request.filesystem_path:
            self.agent_service.assert_connector_allowed(planner_agent, "filesystem")
            filesystem_descriptor = self._filesystem_planning_descriptor(request)
            collected["filesystem"] = filesystem_descriptor
            self._log_planning_descriptor(
                run_id,
                planner_agent,
                "filesystem",
                {"path": request.filesystem_path},
                filesystem_descriptor,
                correlation_id,
            )
            if filesystem_descriptor.get("evidence_deferred"):
                deferred_evidence.append(
                    self._deferred_evidence_entry(
                        connector="filesystem",
                        action_type=filesystem_descriptor["candidate_action"],
                        title=filesystem_descriptor["deferred_title"],
                        reason="Planning did not read local filesystem content before approval.",
                        target={"path": request.filesystem_path},
                    )
                )
        if request.http_url and request.http_method.upper() in {"GET", "HEAD"}:
            self.agent_service.assert_connector_allowed(planner_agent, "http")
            http_descriptor = self._http_planning_descriptor(request)
            collected["http"] = http_descriptor
            self._log_planning_descriptor(
                run_id,
                planner_agent,
                "http",
                {"url": request.http_url, "method": request.http_method.upper()},
                http_descriptor,
                correlation_id,
            )
            deferred_evidence.append(
                self._deferred_evidence_entry(
                    connector="http",
                    action_type=http_descriptor["candidate_action"],
                    title=http_descriptor["deferred_title"],
                    reason="Planning did not perform an outbound network fetch before approval.",
                    target={"url": request.http_url, "method": request.http_method.upper()},
                )
            )
        if request.system_action and request.system_path:
            self.agent_service.assert_connector_allowed(planner_agent, "system")
            system_descriptor = self._system_planning_descriptor(request)
            collected["system"] = system_descriptor
            self._log_planning_descriptor(
                run_id,
                planner_agent,
                "system",
                {"path": request.system_path, "action": request.system_action},
                system_descriptor,
                correlation_id,
            )
            deferred_evidence.append(
                self._deferred_evidence_entry(
                    connector="system",
                    action_type=request.system_action,
                    title=system_descriptor["deferred_title"],
                    reason="Planning did not execute a bounded system read before approval.",
                    target={"path": request.system_path, "action": request.system_action},
                )
            )
        if deferred_evidence:
            collected["deferred_evidence"] = deferred_evidence
        return collected

    def _log_planning_descriptor(
        self,
        run_id: str,
        agent: AgentDefinition,
        connector_name: str,
        payload: dict,
        descriptor: dict,
        correlation_id: str,
    ) -> None:
        self.history_service.log_connector_run(
            run_id=run_id,
            connector=connector_name,
            operation="planning-descriptor",
            status="deferred",
            payload=self.data_governance_service.sanitize_for_history(payload, object_type="connector_input"),
            agent_id=agent.id,
            agent_role=agent.role.value,
            correlation_id=correlation_id,
            output=self.data_governance_service.sanitize_for_history(descriptor, object_type="connector_output"),
        )

    @staticmethod
    def _task_backlog_requested(request: AgentRunRequest) -> bool:
        objective = request.objective.lower()
        return any(
            marker in objective
            for marker in (
                "task backlog",
                "task data",
                "task details",
                "list task",
                "open task",
                "existing task",
                "current task",
            )
        )

    @staticmethod
    def _task_planning_descriptor() -> dict:
        return {
            "collection_mode": "descriptor-only",
            "snapshot_deferred": True,
            "details_redacted": True,
        }

    def _filesystem_planning_descriptor(self, request: AgentRunRequest) -> dict:
        candidate_action = self._inferred_filesystem_action(request, observed_kind=None)
        return {
            "collection_mode": "descriptor-only",
            "path": request.filesystem_path,
            "observed_kind": None,
            "path_hint": "directory" if (request.filesystem_path or "").endswith(("\\", "/")) else "file-or-directory",
            "candidate_action": candidate_action,
            "evidence_deferred": candidate_action in {"filesystem.read_text", "filesystem.list_directory"},
            "deferred_title": (
                f"Gather filesystem evidence with {candidate_action}"
                if candidate_action in {"filesystem.read_text", "filesystem.list_directory"}
                else f"Plan filesystem action {candidate_action}"
            ),
        }

    @staticmethod
    def _http_planning_descriptor(request: AgentRunRequest) -> dict:
        method = request.http_method.upper()
        return {
            "collection_mode": "descriptor-only",
            "url": request.http_url,
            "method": method,
            "candidate_action": f"http.{method.lower()}",
            "request_body_present": bool(request.http_body),
            "headers_present": bool(request.http_headers_text),
            "fetch_deferred": True,
            "deferred_title": f"Fetch HTTP evidence with {method} {request.http_url}",
        }

    @staticmethod
    def _system_planning_descriptor(request: AgentRunRequest) -> dict:
        return {
            "collection_mode": "descriptor-only",
            "path": request.system_path,
            "action": request.system_action,
            "evidence_deferred": True,
            "deferred_title": f"Gather bounded system evidence with {request.system_action}",
        }

    @staticmethod
    def _deferred_evidence_entry(
        *,
        connector: str,
        action_type: str,
        title: str,
        reason: str,
        target: dict,
    ) -> dict:
        return {
            "connector": connector,
            "action_type": action_type,
            "title": title,
            "reason": reason,
            "target": target,
            "status": "pending-approval",
        }

    def _summarize(
        self,
        request: AgentRunRequest,
        local_collected_context: dict,
        provider_collected_context: dict,
        profile: str,
        correlation_id: str,
        local_classification: DataClassification,
        prompt_governance: dict,
    ) -> tuple[str, str, dict]:
        system_prompt = "Produce a concise operator summary for a local-first multi-agent runtime."
        local_prompt = (
            "Summarize the collected local-agent context for a Windows operator. "
            "Highlight risks, side effects, egress implications, and approval-ready actions.\n\n"
            f"Objective:\n{request.objective}\n\nCollected:\n{json_dumps(local_collected_context)}"
        )
        remote_prompt = (
            "Summarize the curated operator-safe context for a Windows operator. "
            "Do not assume access to redacted local runtime, workspace, or system data. "
            "Highlight risks, side effects, egress implications, and approval-ready actions.\n\n"
            f"Objective:\n{request.objective}\n\nCurated context:\n{json_dumps(provider_collected_context)}"
        )
        provider_name = request.provider_name or self.provider_service.resolve_profile_provider(profile)
        prompt_variants = self.data_governance_service.build_prompt_variants(
            prompt_kind="planning-summary",
            local_prompt=local_prompt,
            remote_prompt=remote_prompt,
            local_classification=local_classification,
            outbound_classification=DataClassification.EXTERNAL_OK,
            system_prompt=system_prompt,
            governance=prompt_governance,
        )
        self.audit_service.emit(
            "provider.prompt_prepared",
            {
                "correlation_id": correlation_id,
                "task_type": "planning-summary",
                "provider_name": request.provider_name,
                "profile": profile,
                "governance": prompt_variants["prompt_governance"],
            },
        )
        try:
            response = self.provider_service.complete(
                ProviderRequest(
                    provider_name=request.provider_name,
                    model_name=request.model_name,
                    profile=profile,
                    prompt=local_prompt,
                    system_prompt=system_prompt,
                    data_classification=local_classification.value,
                    task_type="planning-summary",
                    correlation_id=correlation_id,
                    metadata=prompt_variants,
                )
            )
            return response.content, response.provider_name, response.raw_response.get("_routing", {})
        except Exception:
            fragments = [f"Objective: {request.objective}"]
            if "filesystem" in local_collected_context:
                fragments.append("Filesystem context captured.")
            if "http" in local_collected_context:
                fragments.append("HTTP context captured.")
            if "system" in local_collected_context:
                fragments.append("Bounded system context captured.")
            if local_collected_context.get("tasks"):
                fragments.append("Task snapshot captured.")
            fragments.append("All actions remain approval-gated before execution.")
            return " ".join(fragments), (provider_name or "offline-fallback"), {"mode": "offline-fallback"}

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
                    rationale="The objective explicitly asks for task visibility, so planning deferred backlog inspection into this approval-gated read action.",
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
                    rationale="The operator selected a bounded system action, and planning deferred the actual read until approval.",
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

    def execute_planner_node(
        self,
        *,
        node,
        request_payload: dict,
        summary_id: str | None,
        planner_run_id: str,
        correlation_id: str,
        effective_settings,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict:
        request = AgentRunRequest.model_validate(request_payload)
        planner_agent = self.agent_service.get_by_role(AgentRole.PLANNER)
        summary = self.summary_service.get_by_run_id(node.run_id)
        if summary is None:
            raise ValueError(f"Planning summary for run {node.run_id} is missing.")
        summary_id = summary_id or summary.id
        branch_key = node.branch_key or "general"
        provider_profile = node.provider_profile or self._provider_profile_for_role(AgentRole.PLANNER, effective_settings)
        provider_name = request.provider_name or self.provider_service.resolve_profile_provider(provider_profile)
        branch_context = self._branch_context(branch_key, request, summary.collected)
        branch_reasoning = self._branch_reasoning_summary(branch_key, branch_context)
        self.agent_service.update_task_node(
            node.id,
            status="running",
            details=self._sanitize_task_node_details(
                {
                    "branch_key": branch_key,
                    "context_namespace": node.context_namespace,
                    "branch_reasoning": branch_reasoning,
                    "attempt_count": int(node.details.get("attempt_count", 0)) + 1,
                }
            ),
            provider_name=provider_name,
            clear_completion=True,
        )
        self.audit_service.emit(
            "planning.branch_routed",
            {
                "run_id": node.run_id,
                "correlation_id": correlation_id,
                "branch_key": branch_key,
                "provider_profile": provider_profile,
                "provider_name": provider_name,
                "context_namespace": node.context_namespace,
            },
        )
        branch_run = self.agent_service.start_run(
            node.run_id,
            planner_agent,
            input_payload=self._sanitize_agent_payload(
                {
                    "objective": request.objective,
                    "branch_key": branch_key,
                    "context_namespace": node.context_namespace,
                    "branch_context": branch_context,
                },
                object_type="agent_input",
            ),
            provider_profile=provider_profile,
            parent_agent_run_id=planner_run_id,
            correlation_id=correlation_id,
        )
        try:
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            proposals = self._build_branch_proposals(
                branch_key=branch_key,
                run_id=node.run_id,
                request=request,
                summary_id=summary_id,
                collected=summary.collected,
                summary_text=summary.summary_text,
                planning_provider=provider_name,
                planner_agent=planner_agent,
                correlation_id=correlation_id,
            )
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            serialized = [proposal.model_dump(mode="json") for proposal in proposals]
            self.agent_service.complete_run(
                branch_run.id,
                status="completed",
                provider_name=provider_name,
                output_payload=self._sanitize_agent_payload(
                    {
                        "branch_key": branch_key,
                        "proposal_titles": [proposal.title for proposal in proposals],
                        "proposal_count": len(proposals),
                        "result": {"branch_reasoning": branch_reasoning},
                    },
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="completed",
                details=self._sanitize_task_node_details(
                    {
                        "branch_key": branch_key,
                        "proposal_titles": [proposal.title for proposal in proposals],
                        "proposal_count": len(proposals),
                        "branch_reasoning": branch_reasoning,
                        "context_namespace": node.context_namespace,
                    }
                ),
                provider_name=provider_name,
                agent_run_id=branch_run.id,
            )
            return {
                "status": "completed",
                "branch_key": branch_key,
                "proposals": serialized,
                "provider_name": provider_name,
                "provider_profile": provider_profile,
                "agent_run_id": branch_run.id,
                "context_namespace": node.context_namespace,
                "branch_reasoning": branch_reasoning,
            }
        except CancellationRequestedError as exc:
            self.agent_service.complete_run(
                branch_run.id,
                status="cancelled",
                provider_name=provider_name,
                output_payload=self._sanitize_agent_payload(
                    {"error": str(exc), "branch_key": branch_key},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="cancelled",
                details=self._sanitize_task_node_details(
                    {"error": str(exc), "branch_key": branch_key, "cancel_mode": "cooperative"}
                ),
                provider_name=provider_name,
                agent_run_id=branch_run.id,
            )
            raise
        except Exception as exc:
            self.agent_service.complete_run(
                branch_run.id,
                status="failed",
                provider_name=provider_name,
                output_payload=self._sanitize_agent_payload(
                    {"error": str(exc), "branch_key": branch_key},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="failed",
                details=self._sanitize_task_node_details(
                    {"error": str(exc), "branch_key": branch_key}
                ),
                provider_name=provider_name,
                agent_run_id=branch_run.id,
            )
            raise

    def execute_review_node(
        self,
        *,
        node,
        planner_result: dict,
        reviewer_run_id: str,
        correlation_id: str,
        effective_settings,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict:
        reviewer_agent = self.agent_service.get_by_role(AgentRole.REVIEWER)
        branch_key = node.branch_key or planner_result.get("branch_key") or "general"
        provider_profile = node.provider_profile or self._provider_profile_for_role(AgentRole.REVIEWER, effective_settings)
        self.agent_service.update_task_node(
            node.id,
            status="running",
            details=self._sanitize_task_node_details(
                {
                    "branch_key": branch_key,
                    "review_target": planner_result.get("branch_key"),
                    "context_namespace": node.context_namespace,
                    "attempt_count": int(node.details.get("attempt_count", 0)) + 1,
                }
            ),
            clear_completion=True,
        )
        proposals = [ActionProposal.model_validate(payload) for payload in planner_result.get("proposals", [])]
        review_run = self.agent_service.start_run(
            node.run_id,
            reviewer_agent,
            input_payload=self._sanitize_agent_payload(
                {
                    "branch_key": branch_key,
                    "proposal_titles": [proposal.title for proposal in proposals],
                    "context_namespace": node.context_namespace,
                },
                object_type="agent_input",
            ),
            provider_profile=provider_profile,
            parent_agent_run_id=reviewer_run_id,
            correlation_id=correlation_id,
        )
        try:
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            reviewed_proposals = []
            for proposal in proposals:
                self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
                reviewed_proposals.append(self._review_proposal(proposal, reviewer_agent))
            serialized = [proposal.model_dump(mode="json") for proposal in reviewed_proposals]
            self.agent_service.complete_run(
                review_run.id,
                status="completed",
                output_payload=self._sanitize_agent_payload(
                    {
                        "branch_key": branch_key,
                        "proposal_titles": [proposal.title for proposal in reviewed_proposals],
                        "proposal_count": len(reviewed_proposals),
                    },
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="completed",
                details=self._sanitize_task_node_details(
                    {
                        "branch_key": branch_key,
                        "proposal_titles": [proposal.title for proposal in reviewed_proposals],
                        "proposal_count": len(reviewed_proposals),
                        "context_namespace": node.context_namespace,
                    }
                ),
                agent_run_id=review_run.id,
            )
            return {
                "status": "completed",
                "branch_key": branch_key,
                "reviewed_proposals": serialized,
                "agent_run_id": review_run.id,
            }
        except CancellationRequestedError as exc:
            self.agent_service.complete_run(
                review_run.id,
                status="cancelled",
                output_payload=self._sanitize_agent_payload(
                    {"error": str(exc), "branch_key": branch_key},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="cancelled",
                details=self._sanitize_task_node_details(
                    {"error": str(exc), "branch_key": branch_key, "cancel_mode": "cooperative"}
                ),
                agent_run_id=review_run.id,
            )
            raise
        except Exception as exc:
            self.agent_service.complete_run(
                review_run.id,
                status="failed",
                output_payload=self._sanitize_agent_payload(
                    {"error": str(exc), "branch_key": branch_key},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="failed",
                details=self._sanitize_task_node_details(
                    {"error": str(exc), "branch_key": branch_key}
                ),
                agent_run_id=review_run.id,
            )
            raise

    def execute_merge_node(
        self,
        *,
        node,
        request_payload: dict,
        summary_id: str | None,
        reviewer_run_id: str | None,
        planner_run_id: str,
        correlation_id: str,
        effective_settings,
        reviewed_results: list[dict],
        existing_proposal_ids: list[str] | None = None,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict:
        request = AgentRunRequest.model_validate(request_payload)
        summary = self.summary_service.get_by_run_id(node.run_id)
        if summary is None:
            raise ValueError(f"Planning summary for run {node.run_id} is missing.")
        summary_id = summary_id or summary.id
        self.agent_service.update_task_node(
            node.id,
            status="running",
            details=self._sanitize_task_node_details(
                {
                    "merge_key": node.merge_key,
                    "reviewed_branch_count": len(reviewed_results),
                    "attempt_count": int(node.details.get("attempt_count", 0)) + 1,
                }
            ),
            clear_completion=True,
        )
        if existing_proposal_ids:
            created = [self.proposal_service.get(proposal_id) for proposal_id in existing_proposal_ids]
        else:
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            reviewed = [
                ActionProposal.model_validate(payload)
                for result in reviewed_results
                for payload in result.get("reviewed_proposals", [])
            ]
            if not reviewed:
                planner_agent = self.agent_service.get_by_role(AgentRole.PLANNER)
                reviewer_agent = self.agent_service.get_by_role(AgentRole.REVIEWER)
                fallback_provider = request.provider_name or self.provider_service.resolve_profile_provider(
                    self._provider_profile_for_role(AgentRole.PLANNER, effective_settings)
                )
                raw_proposals = self._build_proposals(
                    run_id=node.run_id,
                    request=request,
                    summary_id=summary_id,
                    collected=summary.collected,
                    summary_text=summary.summary_text,
                    planning_provider=fallback_provider,
                    planner_agent=planner_agent,
                    correlation_id=correlation_id,
                )
                reviewed = [self._review_proposal(proposal, reviewer_agent) for proposal in raw_proposals]
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            created = self.materialize_reviewed_proposals(
                run_id=node.run_id,
                reviewed=reviewed,
                planner_run_id=planner_run_id,
                effective_settings=effective_settings,
                correlation_id=correlation_id,
            )
        self.agent_service.complete_task_node(
            node.id,
            status="completed",
            details=self._sanitize_task_node_details(
                {
                    "merge_key": node.merge_key,
                    "proposal_count": len(created),
                    "proposal_ids": [proposal.id for proposal in created],
                }
            ),
            agent_run_id=reviewer_run_id,
        )
        return {
            "status": "completed",
            "proposal_ids": [proposal.id for proposal in created],
            "proposal_titles": [proposal.title for proposal in created],
        }

    def execute_reporter_node(
        self,
        *,
        node,
        request_payload: dict,
        proposal_ids: list[str],
        reporter_run_id: str,
        correlation_id: str,
        effective_settings,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict:
        request = AgentRunRequest.model_validate(request_payload)
        summary = self.summary_service.get_by_run_id(node.run_id)
        if summary is None:
            raise ValueError(f"Planning summary for run {node.run_id} is missing.")
        proposals = [self.proposal_service.get(proposal_id) for proposal_id in proposal_ids]
        try:
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            operator_summary, reporter_provider, reporter_routing = self._report_plan(
                request,
                proposals,
                summary,
                self._provider_profile_for_role(AgentRole.REPORTER, effective_settings),
                correlation_id,
            )
            self._execution_checkpoint(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            self.agent_service.complete_run(
                reporter_run_id,
                status="completed",
                provider_name=reporter_provider,
                output_payload=self._sanitize_agent_payload(
                    {
                        "operator_summary": operator_summary,
                        "proposal_ids": proposal_ids,
                        "provider_routing": reporter_routing,
                    },
                    object_type="agent_output",
                ),
            )
            if node.handoff_id:
                self.agent_service.complete_handoff(node.handoff_id)
            self.agent_service.complete_task_node(
                node.id,
                status="completed",
                details=self._sanitize_task_node_details(
                    {
                        "operator_summary": operator_summary,
                        "memory_namespace": self.agent_service.get_by_role(AgentRole.REPORTER).memory_namespace,
                        "provider_routing": reporter_routing,
                    }
                ),
                agent_run_id=reporter_run_id,
                handoff_id=node.handoff_id,
                provider_name=reporter_provider,
            )
            return {
                "status": "completed",
                "operator_summary": operator_summary,
                "provider_name": reporter_provider,
                "provider_routing": reporter_routing,
            }
        except CancellationRequestedError as exc:
            self.agent_service.complete_run(
                reporter_run_id,
                status="cancelled",
                output_payload=self._sanitize_agent_payload(
                    {"error": str(exc), "proposal_ids": proposal_ids},
                    object_type="agent_output",
                ),
            )
            self.agent_service.complete_task_node(
                node.id,
                status="cancelled",
                details=self._sanitize_task_node_details(
                    {"error": str(exc), "cancel_mode": "cooperative"}
                ),
                agent_run_id=reporter_run_id,
                handoff_id=node.handoff_id,
            )
            raise

    def materialize_reviewed_proposals(
        self,
        *,
        run_id: str,
        reviewed: list[ActionProposal],
        planner_run_id: str,
        effective_settings,
        correlation_id: str,
    ):
        created = self.proposal_service.create_many(reviewed)
        nodes = self.agent_service.list_task_nodes(run_id)
        objective_node = next((node for node in nodes if node.role == AgentRole.SUPERVISOR), None)
        planner_agent = self.agent_service.get_by_role(AgentRole.PLANNER)
        executor_agent = self.agent_service.get_by_role(AgentRole.EXECUTOR)
        proposal_dependency = [
            node.id for node in nodes if node.role == AgentRole.REVIEWER and node.node_type in {"review", "merge"}
        ]
        existing_nodes = {(node.proposal_id, node.role.value, node.node_type) for node in nodes if node.proposal_id}
        for proposal in created:
            if (proposal.id, AgentRole.PLANNER.value, "proposal") in existing_nodes:
                continue
            executor_context_namespace = f"{executor_agent.memory_namespace}:{run_id}:{proposal.id}:{proposal.connector}"
            proposal_parent = self._proposal_parent_node(proposal.connector, nodes, objective_node.id if objective_node else "")
            proposal_node = self.agent_service.create_task_node(
                run_id,
                role=AgentRole.PLANNER,
                node_type="proposal",
                title=proposal.title,
                details=self._sanitize_task_node_details(
                    {
                        "proposal_id": proposal.id,
                        "connector": proposal.connector,
                        "action_type": proposal.action_type,
                        "risk_level": proposal.risk_level.value,
                        "status": proposal.status.value,
                        "requires_approval": proposal.requires_approval,
                        "created_by": proposal.created_by_agent_role,
                        "reviewed_by": proposal.reviewed_by_agent_role,
                        "approval_wait": proposal.requires_approval and proposal.status.value in {"pending", "stale"},
                        "capabilities": planner_agent.capabilities,
                        "allowed_connectors": planner_agent.allowed_connectors,
                        "branch_key": proposal.connector,
                        "context_namespace": f"{planner_agent.memory_namespace}:{run_id}:{proposal.connector}",
                    }
                    | self._agent_work_area_details(
                        run_id=run_id,
                        agent=planner_agent,
                        context_namespace=f"{planner_agent.memory_namespace}:{run_id}:{proposal.connector}",
                        branch_key=proposal.connector,
                        promotion_target_hint=proposal.payload.get("path")
                        or proposal.payload.get("destination_path")
                        or proposal.payload.get("url"),
                    )
                ),
                status="waiting_approval" if proposal.requires_approval and proposal.status.value in {"pending", "stale"} else proposal.status.value,
                parent_task_node_id=proposal_parent,
                proposal_id=proposal.id,
                branch_key=proposal.connector,
                context_namespace=f"{planner_agent.memory_namespace}:{run_id}:{proposal.connector}",
                agent=planner_agent,
                agent_run_id=planner_run_id,
                provider_profile=self._provider_profile_for_role(AgentRole.PLANNER, effective_settings),
                provider_name=proposal.provider_name,
                correlation_id=correlation_id,
                depends_on=proposal_dependency,
            )
            self.agent_service.create_task_node(
                run_id,
                role=AgentRole.EXECUTOR,
                node_type="execute",
                title=f"Execute approved action for {proposal.title}",
                details=self._sanitize_task_node_details(
                    {
                        "proposal_id": proposal.id,
                        "connector": proposal.connector,
                        "action_type": proposal.action_type,
                        "memory_namespace": executor_agent.memory_namespace,
                        "context_namespace": executor_context_namespace,
                        "capabilities": executor_agent.capabilities,
                        "allowed_connectors": executor_agent.allowed_connectors,
                        "approval_wait": proposal.requires_approval,
                    }
                    | self._agent_work_area_details(
                        run_id=run_id,
                        agent=executor_agent,
                        context_namespace=executor_context_namespace,
                        branch_key=proposal.connector,
                        proposal_id=proposal.id,
                        promotion_target_hint=proposal.payload.get("path")
                        or proposal.payload.get("destination_path")
                        or proposal.payload.get("url"),
                    )
                ),
                status="waiting_approval" if proposal.requires_approval else "ready",
                parent_task_node_id=proposal_node.id,
                proposal_id=proposal.id,
                branch_key=proposal.connector,
                context_namespace=executor_context_namespace,
                agent=executor_agent,
                provider_profile=self._provider_profile_for_role(AgentRole.EXECUTOR, effective_settings),
                correlation_id=correlation_id,
                depends_on=[proposal_node.id],
            )
        return created

    def _schedule_planner_branches(
        self,
        *,
        run_id: str,
        request: AgentRunRequest,
        planner_run_id: str,
        planner_agent: AgentDefinition,
        planner_nodes,
        collected: dict,
        summary_id: str,
        summary_text: str,
        effective_settings,
        correlation_id: str,
    ) -> tuple[list[ActionProposal], dict[str, dict]]:
        if not planner_nodes:
            return [], {}
        scheduler = TaskGraphScheduler(max_workers=min(3, len(planner_nodes)))

        def handler(node, dependency_results):
            branch_key = node.branch_key or "general"
            provider_profile = node.provider_profile or self._provider_profile_for_role(AgentRole.PLANNER, effective_settings)
            provider_name = request.provider_name or self.provider_service.resolve_profile_provider(provider_profile)
            branch_context = self._branch_context(branch_key, request, collected)
            branch_reasoning = self._branch_reasoning_summary(branch_key, branch_context)
            self.agent_service.update_task_node(
                node.id,
                status="running",
                details=self._sanitize_task_node_details(
                    {
                        "branch_key": branch_key,
                        "context_namespace": node.context_namespace,
                        "dependency_count": len(dependency_results),
                        "branch_reasoning": branch_reasoning,
                    }
                ),
                provider_name=provider_name,
            )
            self.audit_service.emit(
                "planning.branch_routed",
                {
                    "run_id": run_id,
                    "correlation_id": correlation_id,
                    "branch_key": branch_key,
                    "provider_profile": provider_profile,
                    "provider_name": provider_name,
                    "context_namespace": node.context_namespace,
                },
            )
            branch_run = self.agent_service.start_run(
                run_id,
                planner_agent,
                input_payload=self._sanitize_agent_payload(
                    {
                        "objective": request.objective,
                        "branch_key": branch_key,
                        "context_namespace": node.context_namespace,
                        "branch_context": branch_context,
                    },
                    object_type="agent_input",
                ),
                provider_profile=provider_profile,
                parent_agent_run_id=planner_run_id,
                correlation_id=correlation_id,
            )
            try:
                proposals = self._build_branch_proposals(
                    branch_key=branch_key,
                    run_id=run_id,
                    request=request,
                    summary_id=summary_id,
                    collected=collected,
                    summary_text=summary_text,
                    planning_provider=provider_name,
                    planner_agent=planner_agent,
                    correlation_id=correlation_id,
                )
                self.agent_service.complete_run(
                    branch_run.id,
                    status="completed",
                    provider_name=provider_name,
                    output_payload=self._sanitize_agent_payload(
                        {
                            "branch_key": branch_key,
                            "proposal_titles": [proposal.title for proposal in proposals],
                            "proposal_count": len(proposals),
                            "result": {"branch_reasoning": branch_reasoning},
                        },
                        object_type="agent_output",
                    ),
                )
                self.agent_service.complete_task_node(
                    node.id,
                    status="completed",
                    details=self._sanitize_task_node_details(
                        {
                            "branch_key": branch_key,
                            "proposal_titles": [proposal.title for proposal in proposals],
                            "proposal_count": len(proposals),
                            "branch_reasoning": branch_reasoning,
                            "context_namespace": node.context_namespace,
                        }
                    ),
                    provider_name=provider_name,
                    agent_run_id=branch_run.id,
                )
                return {
                    "status": "completed",
                    "branch_key": branch_key,
                    "proposals": proposals,
                    "provider_name": provider_name,
                    "provider_profile": provider_profile,
                    "agent_run_id": branch_run.id,
                    "context_namespace": node.context_namespace,
                    "branch_reasoning": branch_reasoning,
                }
            except Exception as exc:
                self.agent_service.complete_run(
                    branch_run.id,
                    status="failed",
                    provider_name=provider_name,
                    output_payload=self._sanitize_agent_payload(
                        {"error": str(exc), "branch_key": branch_key},
                        object_type="agent_output",
                    ),
                )
                self.agent_service.complete_task_node(
                    node.id,
                    status="failed",
                    details=self._sanitize_task_node_details(
                        {"error": str(exc), "branch_key": branch_key}
                    ),
                    provider_name=provider_name,
                    agent_run_id=branch_run.id,
                )
                raise

        scheduled = scheduler.execute(planner_nodes, handler)
        proposals: list[ActionProposal] = []
        results: dict[str, dict] = {}
        for node in planner_nodes:
            result = scheduled["results"].get(node.id, {})
            results[node.id] = result
            if result.get("status") == "completed":
                proposals.extend(result.get("proposals", []))
        return proposals, results

    def _schedule_review_branches(
        self,
        *,
        review_nodes,
        merge_node,
        reviewer_agent: AgentDefinition,
        reviewer_run_id: str,
        planner_branch_results: dict[str, dict],
        correlation_id: str,
        effective_settings,
    ) -> list[ActionProposal]:
        nodes = list(review_nodes)
        if merge_node is not None:
            nodes.append(merge_node)
        if not nodes:
            return []

        initial_states = {
            node_id: "completed"
            for node_id, result in planner_branch_results.items()
            if result.get("status") == "completed"
        }
        initial_results = {
            node_id: result
            for node_id, result in planner_branch_results.items()
            if result.get("status") == "completed"
        }
        scheduler = TaskGraphScheduler(max_workers=min(3, max(1, len(review_nodes))))

        def handler(node, dependency_results):
            if node.node_type == "merge":
                merged_proposals = []
                branch_keys: list[str] = []
                for result in dependency_results:
                    merged_proposals.extend(result.get("reviewed_proposals", []))
                    if result.get("branch_key"):
                        branch_keys.append(result["branch_key"])
                self.agent_service.update_task_node(
                    node.id,
                    status="running",
                    details=self._sanitize_task_node_details(
                        {
                            "merge_key": node.merge_key,
                            "reviewed_branch_count": len(branch_keys),
                        }
                    ),
                )
                self.agent_service.complete_task_node(
                    node.id,
                    status="completed",
                    details=self._sanitize_task_node_details(
                        {
                            "merge_key": node.merge_key,
                            "reviewed_branch_count": len(branch_keys),
                            "proposal_count": len(merged_proposals),
                        }
                    ),
                    agent_run_id=reviewer_run_id,
                )
                return {
                    "status": "completed",
                    "branch_key": node.branch_key,
                    "reviewed_proposals": merged_proposals,
                    "merge_key": node.merge_key,
                }

            branch_result = dependency_results[0] if dependency_results else {}
            branch_key = node.branch_key or branch_result.get("branch_key") or "general"
            provider_profile = node.provider_profile or self._provider_profile_for_role(AgentRole.REVIEWER, effective_settings)
            self.agent_service.update_task_node(
                node.id,
                status="running",
                details=self._sanitize_task_node_details(
                    {
                        "branch_key": branch_key,
                        "review_target": branch_result.get("branch_key"),
                        "context_namespace": node.context_namespace,
                    }
                ),
            )
            review_run = self.agent_service.start_run(
                branch_result.get("proposals", [])[0].run_id if branch_result.get("proposals") else node.run_id,
                reviewer_agent,
                input_payload=self._sanitize_agent_payload(
                    {
                        "branch_key": branch_key,
                        "proposal_titles": [proposal.title for proposal in branch_result.get("proposals", [])],
                        "context_namespace": node.context_namespace,
                    },
                    object_type="agent_input",
                ),
                provider_profile=provider_profile,
                parent_agent_run_id=reviewer_run_id,
                correlation_id=correlation_id,
            )
            try:
                reviewed_proposals = [
                    self._review_proposal(proposal, reviewer_agent)
                    for proposal in branch_result.get("proposals", [])
                ]
                self.agent_service.complete_run(
                    review_run.id,
                    status="completed",
                    output_payload=self._sanitize_agent_payload(
                        {
                            "branch_key": branch_key,
                            "proposal_ids": [proposal.id for proposal in reviewed_proposals if getattr(proposal, "id", None)],
                            "proposal_titles": [proposal.title for proposal in reviewed_proposals],
                        },
                        object_type="agent_output",
                    ),
                )
                self.agent_service.complete_task_node(
                    node.id,
                    status="completed",
                    details=self._sanitize_task_node_details(
                        {
                            "branch_key": branch_key,
                            "proposal_titles": [proposal.title for proposal in reviewed_proposals],
                            "proposal_count": len(reviewed_proposals),
                            "context_namespace": node.context_namespace,
                        }
                    ),
                    agent_run_id=review_run.id,
                )
                return {
                    "status": "completed",
                    "branch_key": branch_key,
                    "reviewed_proposals": reviewed_proposals,
                    "agent_run_id": review_run.id,
                }
            except Exception as exc:
                self.agent_service.complete_run(
                    review_run.id,
                    status="failed",
                    output_payload=self._sanitize_agent_payload(
                        {"error": str(exc), "branch_key": branch_key},
                        object_type="agent_output",
                    ),
                )
                self.agent_service.complete_task_node(
                    node.id,
                    status="failed",
                    details=self._sanitize_task_node_details(
                        {"error": str(exc), "branch_key": branch_key}
                    ),
                    agent_run_id=review_run.id,
                )
                raise

        scheduled = scheduler.execute(
            nodes,
            handler,
            initial_states=initial_states,
            initial_results=initial_results,
        )
        if merge_node is not None:
            merge_result = scheduled["results"].get(merge_node.id, {})
            if merge_result.get("status") == "completed":
                return merge_result.get("reviewed_proposals", [])
        reviewed: list[ActionProposal] = []
        for node in review_nodes:
            result = scheduled["results"].get(node.id, {})
            reviewed.extend(result.get("reviewed_proposals", []))
        return reviewed

    def _branch_context(self, branch_key: str, request: AgentRunRequest, collected: dict) -> dict:
        context = {"objective": request.objective, "branch_key": branch_key}
        if branch_key == "filesystem":
            context["filesystem"] = collected.get("filesystem")
            context["requested_path"] = request.filesystem_path
        elif branch_key == "http":
            context["http"] = collected.get("http")
            context["requested_url"] = request.http_url
            context["method"] = request.http_method.upper()
        elif branch_key == "system":
            context["system"] = collected.get("system")
            context["system_action"] = request.system_action
        elif branch_key == "task":
            context["tasks"] = collected.get("tasks")
            context["task_title"] = request.task_title
            context["task_details_present"] = bool(request.task_details)
        return self.data_governance_service.sanitize_for_history(context, object_type="agent_input")

    def _branch_reasoning_summary(self, branch_key: str, branch_context: dict) -> str:
        if branch_key == "filesystem":
            return "Planner separated the filesystem branch and deferred file or directory inspection until an approved read action runs."
        if branch_key == "http":
            return "Planner separated the HTTP branch and deferred the outbound fetch until an approved connector action runs."
        if branch_key == "system":
            return "Planner separated the bounded system branch and deferred schema-driven reads until approval."
        if branch_key == "task":
            return "Planner separated the local task branch and deferred backlog inspection until an approved task read runs."
        return f"Planner separated branch {branch_key} for bounded orchestration."

    def _build_branch_proposals(
        self,
        *,
        branch_key: str,
        run_id: str,
        request: AgentRunRequest,
        summary_id: str,
        collected: dict,
        summary_text: str,
        planning_provider: str | None,
        planner_agent: AgentDefinition,
        correlation_id: str,
    ) -> list[ActionProposal]:
        if branch_key == "filesystem" and request.filesystem_path:
            return self._filesystem_proposals(
                run_id,
                request,
                summary_id,
                collected,
                planning_provider,
                planner_agent,
                correlation_id,
            )
        if branch_key == "http" and request.http_url:
            return [
                self._http_proposal(
                    run_id,
                    request,
                    summary_id,
                    planning_provider,
                    planner_agent,
                    correlation_id,
                )
            ]
        if branch_key == "task":
            proposals: list[ActionProposal] = []
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
                        rationale="The objective explicitly asks for task visibility, so planning deferred backlog inspection into this approval-gated read action.",
                        provider_name=planning_provider,
                        summary_id=summary_id,
                        created_by_agent_id=planner_agent.id,
                        created_by_agent_role=AgentRole.PLANNER.value,
                        correlation_id=correlation_id,
                        data_classification=DataClassification.LOCAL_ONLY,
                    )
                )
            return proposals
        if branch_key == "system" and request.system_action:
            self.agent_service.assert_connector_allowed(planner_agent, "system")
            return [
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="system",
                    action_type=request.system_action,
                    title=f"Run bounded system action {request.system_action}",
                    description="Execute a schema-driven read-only system action.",
                    payload={"path": request.system_path},
                    rationale="The operator selected a bounded system action, and planning deferred the actual read until approval.",
                    provider_name=planning_provider,
                    summary_id=summary_id,
                    created_by_agent_id=planner_agent.id,
                    created_by_agent_role=AgentRole.PLANNER.value,
                    correlation_id=correlation_id,
                    data_classification=DataClassification.LOCAL_ONLY,
                )
            ]
        return []

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
        observed_kind = (collected.get("filesystem") or {}).get("observed_kind")
        proposals: list[ActionProposal] = []
        classification = DataClassification.RESTRICTED if request.file_content is not None else DataClassification.LOCAL_ONLY
        self.agent_service.assert_connector_allowed(planner_agent, "filesystem")
        inferred_action = self._inferred_filesystem_action(request, observed_kind=observed_kind)

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

        if inferred_action == "filesystem.delete_path":
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
        elif inferred_action == "filesystem.make_directory":
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
        elif inferred_action == "filesystem.list_directory":
            proposals.append(
                ActionProposal(
                    run_id=run_id,
                    objective=request.objective,
                    connector="filesystem",
                    action_type="filesystem.list_directory",
                    title=f"List directory {path}",
                    description="List directory entries within the configured filesystem allowlist.",
                    payload={"path": path},
                    rationale="Planning deferred directory inspection until approval, so this action gathers the needed listing evidence inside the bounded filesystem connector.",
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
                    rationale="Planning deferred file-content inspection until approval, so this action gathers the needed read evidence inside the bounded filesystem connector.",
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
            rationale=(
                "The operator provided an explicit target URL."
                if method not in {"GET", "HEAD"}
                else "Planning did not fetch the remote response before approval, so this bounded request gathers the needed evidence when approved."
            ),
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
        summary,
        profile: str,
        correlation_id: str,
    ) -> tuple[str, str, dict]:
        proposal_payloads = [proposal.model_dump(mode="json") for proposal in proposals]
        local_report_context, remote_report_context, report_governance = (
            self.data_governance_service.build_report_context_views(
                request.model_dump(mode="json"),
                proposal_payloads,
                summary.summary_text,
                summary_classification=summary.data_classification,
                summary_lineage=summary.lineage,
                outbound_summary_text=summary.outbound_summary_text,
            )
        )
        system_prompt = "You are the reporter agent in a Windows-first local control console."
        local_prompt = (
            "Summarize this local multi-agent plan for the operator. "
            "Explain why each action is proposed, what requires approval, and the most important risks.\n\n"
            f"Objective:\n{request.objective}\n\nPlan context:\n{json_dumps(local_report_context)}"
        )
        remote_prompt = (
            "Summarize this reviewed local multi-agent plan for the operator using only curated outbound-safe context. "
            "Explain why each action is proposed, what requires approval, and the most important risks.\n\n"
            f"Objective:\n{request.objective}\n\nPlan context:\n{json_dumps(remote_report_context)}"
        )
        provider_name = request.provider_name or self.provider_service.resolve_profile_provider(profile)
        prompt_variants = self.data_governance_service.build_prompt_variants(
            prompt_kind="report-plan",
            local_prompt=local_prompt,
            remote_prompt=remote_prompt,
            local_classification=DataClassification.LOCAL_ONLY,
            outbound_classification=DataClassification.EXTERNAL_OK,
            system_prompt=system_prompt,
            governance=report_governance,
        )
        self.audit_service.emit(
            "provider.prompt_prepared",
            {
                "correlation_id": correlation_id,
                "task_type": "report-plan",
                "provider_name": request.provider_name,
                "profile": profile,
                "governance": prompt_variants["prompt_governance"],
            },
        )
        try:
            response = self.provider_service.complete(
                ProviderRequest(
                    provider_name=request.provider_name,
                    model_name=request.model_name,
                    profile=profile,
                    prompt=local_prompt,
                    system_prompt=system_prompt,
                    data_classification=DataClassification.LOCAL_ONLY.value,
                    task_type="report-plan",
                    correlation_id=correlation_id,
                    metadata=prompt_variants,
                )
            )
            return response.content, response.provider_name, response.raw_response.get("_routing", {})
        except Exception:
            deferred = summary.collected.get("deferred_evidence") or []
            proposal_count = len(proposals)
            fragments = [
                "Plan ready.",
                (
                    f"{proposal_count} bounded action(s) are ready for operator review."
                    if proposal_count
                    else "No bounded actions were generated yet."
                ),
                "Local-only planning context remained on this machine.",
                "All actions remain approval-gated before execution.",
            ]
            if deferred:
                fragments.append(
                    f"Planning deferred {len(deferred)} evidence-gathering step(s) until approval."
                )
            return (
                " ".join(fragments),
                provider_name or "offline-fallback",
                {"mode": "offline-fallback"},
            )

    @staticmethod
    def _execution_checkpoint(*, heartbeat_callback=None, cancel_check=None) -> None:
        if heartbeat_callback:
            heartbeat_callback()
        if cancel_check:
            cancel_check()

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
        subtasks: list[dict[str, str]] = []
        if request.filesystem_path:
            subtasks.append(
                {
                    "role": AgentRole.PLANNER.value,
                    "title": f"Plan filesystem evidence for {request.filesystem_path}",
                    "node_type": "collect",
                    "branch_key": "filesystem",
                }
            )
        if request.http_url:
            subtasks.append(
                {
                    "role": AgentRole.PLANNER.value,
                    "title": f"Prepare HTTP action for {request.http_url}",
                    "node_type": "plan",
                    "branch_key": "http",
                }
            )
        if request.system_action:
            subtasks.append(
                {
                    "role": AgentRole.PLANNER.value,
                    "title": f"Prepare bounded system action {request.system_action}",
                    "node_type": "plan",
                    "branch_key": "system",
                }
            )
        if request.task_title:
            subtasks.append(
                {
                    "role": AgentRole.PLANNER.value,
                    "title": f"Create local task {request.task_title}",
                    "node_type": "plan",
                    "branch_key": "task",
                }
            )
        subtasks.append(
            {
                "role": AgentRole.REVIEWER.value,
                "title": "Evaluate risk, policy, and egress",
                "node_type": "review",
            }
        )
        subtasks.append(
            {
                "role": AgentRole.REPORTER.value,
                "title": "Prepare operator-facing plan summary",
                "node_type": "report",
            }
        )
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
        if "filesystem" in collected or "system" in collected or "tasks" in collected:
            return DataClassification.LOCAL_ONLY
        return DataClassification.EXTERNAL_OK

    @staticmethod
    def _inferred_filesystem_action(request: AgentRunRequest, observed_kind: str | None) -> str:
        path = request.filesystem_path or ""
        lower_objective = request.objective.lower()
        if request.file_content is not None:
            return "filesystem.append_text" if "append" in lower_objective else "filesystem.write_text"
        if "delete" in lower_objective:
            return "filesystem.delete_path"
        if "create folder" in lower_objective or "create directory" in lower_objective or "mkdir" in lower_objective:
            return "filesystem.make_directory"
        if observed_kind == "directory" or path.endswith(("\\", "/")) or "list" in lower_objective:
            return "filesystem.list_directory"
        return "filesystem.read_text"

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

    def _ensure_graph_planning_nodes(
        self,
        *,
        run_id,
        request: AgentRunRequest,
        objective_node,
        subtasks,
        correlation_id,
        settings,
    ):
        nodes = self.agent_service.list_task_nodes(run_id)
        summary_node = next(
            (node for node in nodes if node.role == AgentRole.PLANNER and node.node_type == "summary"),
            None,
        )
        created_nodes = []
        planner_agent = self.agent_service.get_by_role(AgentRole.PLANNER)
        if summary_node is None:
            summary_namespace = f"{planner_agent.memory_namespace}:{run_id}:summary"
            summary_node = self.agent_service.create_task_node(
                run_id,
                role=AgentRole.PLANNER,
                node_type="summary",
                title=f"Generate planning summary for {request.objective}",
                details=self._sanitize_task_node_details(
                    {
                        "sequence": 1,
                        "phase": "planning-summary",
                        "memory_namespace": planner_agent.memory_namespace,
                        "planned_provider_profile": self._provider_profile_for_role(AgentRole.PLANNER, settings),
                        "capabilities": planner_agent.capabilities,
                        "allowed_connectors": planner_agent.allowed_connectors,
                        "input_source": "objective-decomposition",
                        "context_namespace": summary_namespace,
                    }
                    | self._agent_work_area_details(
                        run_id=run_id,
                        agent=planner_agent,
                        context_namespace=summary_namespace,
                        branch_key="summary",
                    )
                ),
                status="blocked",
                parent_task_node_id=objective_node.id,
                branch_key="summary",
                context_namespace=summary_namespace,
                agent=planner_agent,
                provider_profile=self._provider_profile_for_role(AgentRole.PLANNER, settings),
                correlation_id=correlation_id,
                depends_on=[objective_node.id],
            )
            created_nodes.append(summary_node)
        branch_nodes = [
            node
            for node in self.agent_service.list_task_nodes(run_id)
            if node.parent_task_node_id == objective_node.id and node.node_type != "summary"
        ]
        if not branch_nodes:
            created_nodes.extend(
                self._create_subtask_nodes(
                    run_id,
                    subtasks,
                    objective_node.id,
                    summary_node.id,
                    correlation_id,
                    settings,
                    start_sequence=2,
                )
            )
        return summary_node, created_nodes

    def _create_subtask_nodes(
        self,
        run_id,
        subtasks,
        parent_node_id,
        planning_dependency_node_id,
        correlation_id,
        settings,
        start_sequence=1,
    ):
        nodes = []
        planner_dependencies = [planning_dependency_node_id]
        planner_nodes = []
        review_nodes = []
        sequence = start_sequence

        for subtask in subtasks:
            role = AgentRole(subtask["role"])
            agent = self.agent_service.get_by_role(role)
            if role == AgentRole.PLANNER:
                branch_key = subtask.get("branch_key") or subtask["title"].split(" ", 1)[0].lower()
                context_namespace = f"{agent.memory_namespace}:{run_id}:{sequence}"
                node = self.agent_service.create_task_node(
                    run_id,
                    role=role,
                    node_type=subtask.get("node_type", "subtask"),
                    title=subtask["title"],
                    details=self._sanitize_task_node_details(
                        {
                            "sequence": sequence,
                            "phase": "planner",
                            "memory_namespace": agent.memory_namespace,
                            "planned_provider_profile": self._provider_profile_for_role(role, settings),
                            "capabilities": agent.capabilities,
                            "allowed_connectors": agent.allowed_connectors,
                            "input_source": "supervisor-decomposition",
                            "branch_key": branch_key,
                            "context_namespace": context_namespace,
                            "connector_hint": branch_key,
                        }
                        | self._agent_work_area_details(
                            run_id=run_id,
                            agent=agent,
                            context_namespace=context_namespace,
                            branch_key=branch_key,
                        )
                    ),
                    status="ready",
                    parent_task_node_id=parent_node_id,
                    branch_key=branch_key,
                    context_namespace=context_namespace,
                    agent=agent,
                    provider_profile=self._provider_profile_for_role(role, settings),
                    correlation_id=correlation_id,
                    depends_on=planner_dependencies,
                )
                planner_nodes.append(node)
                nodes.append(node)
                sequence += 1

        reviewer_agent = self.agent_service.get_by_role(AgentRole.REVIEWER)
        for planner_node in planner_nodes:
            branch_key = planner_node.branch_key or planner_node.details.get("branch_key") or planner_node.node_type
            node = self.agent_service.create_task_node(
                run_id,
                role=AgentRole.REVIEWER,
                node_type="review",
                title=f"Review branch: {planner_node.title}",
                details=self._sanitize_task_node_details(
                    {
                        "sequence": sequence,
                        "phase": "review",
                        "memory_namespace": f"{reviewer_agent.memory_namespace}:{run_id}:{branch_key}",
                        "planned_provider_profile": self._provider_profile_for_role(AgentRole.REVIEWER, settings),
                        "capabilities": reviewer_agent.capabilities,
                        "allowed_connectors": reviewer_agent.allowed_connectors,
                        "branch_key": branch_key,
                        "connector_hint": planner_node.details.get("connector_hint"),
                        "review_target": planner_node.title,
                    }
                    | self._agent_work_area_details(
                        run_id=run_id,
                        agent=reviewer_agent,
                        context_namespace=f"{reviewer_agent.memory_namespace}:{run_id}:{branch_key}",
                        branch_key=branch_key,
                    )
                ),
                status="blocked",
                parent_task_node_id=planner_node.id,
                branch_key=branch_key,
                context_namespace=f"{reviewer_agent.memory_namespace}:{run_id}:{branch_key}",
                agent=reviewer_agent,
                provider_profile=self._provider_profile_for_role(AgentRole.REVIEWER, settings),
                correlation_id=correlation_id,
                depends_on=[planner_node.id],
            )
            review_nodes.append(node)
            nodes.append(node)
            sequence += 1

        reviewer_dependencies = [node.id for node in review_nodes] or [parent_node_id]
        merge_node = self.agent_service.create_task_node(
            run_id,
            role=AgentRole.REVIEWER,
            node_type="merge",
            title="Merge reviewed branches",
            details=self._sanitize_task_node_details(
                {
                    "sequence": sequence,
                    "phase": "review-merge",
                    "memory_namespace": f"{reviewer_agent.memory_namespace}:{run_id}:merge",
                    "planned_provider_profile": self._provider_profile_for_role(AgentRole.REVIEWER, settings),
                    "branch_count": len(review_nodes),
                }
                | self._agent_work_area_details(
                    run_id=run_id,
                    agent=reviewer_agent,
                    context_namespace=f"{reviewer_agent.memory_namespace}:{run_id}:merge",
                    branch_key="review-merge",
                )
            ),
            status="blocked",
            parent_task_node_id=parent_node_id,
            branch_key="review-merge",
            context_namespace=f"{reviewer_agent.memory_namespace}:{run_id}:merge",
            merge_key="review-merge",
            agent=reviewer_agent,
            provider_profile=self._provider_profile_for_role(AgentRole.REVIEWER, settings),
            correlation_id=correlation_id,
            depends_on=reviewer_dependencies,
        )
        nodes.append(merge_node)
        sequence += 1

        reporter_dependencies = [merge_node.id]
        for subtask in subtasks:
            role = AgentRole(subtask["role"])
            if role != AgentRole.REPORTER:
                continue
            agent = self.agent_service.get_by_role(role)
            node = self.agent_service.create_task_node(
                run_id,
                role=role,
                node_type=subtask.get("node_type", "subtask"),
                title=subtask["title"],
                details=self._sanitize_task_node_details(
                    {
                        "sequence": sequence,
                        "phase": "report",
                        "memory_namespace": agent.memory_namespace,
                        "planned_provider_profile": self._provider_profile_for_role(role, settings),
                        "capabilities": agent.capabilities,
                        "allowed_connectors": agent.allowed_connectors,
                        "depends_on_merge": merge_node.id,
                    }
                    | self._agent_work_area_details(
                        run_id=run_id,
                        agent=agent,
                        context_namespace=f"{agent.memory_namespace}:{run_id}:reporting",
                        branch_key="reporting",
                    )
                ),
                status="blocked",
                parent_task_node_id=parent_node_id,
                branch_key="reporting",
                context_namespace=f"{agent.memory_namespace}:{run_id}:reporting",
                merge_key="review-merge",
                agent=agent,
                provider_profile=self._provider_profile_for_role(role, settings),
                correlation_id=correlation_id,
                depends_on=reporter_dependencies,
            )
            nodes.append(node)
            sequence += 1
        return nodes

    def _agent_work_area_details(
        self,
        *,
        run_id: str,
        agent: AgentDefinition,
        context_namespace: str,
        branch_key: str | None,
        proposal_id: str | None = None,
        promotion_target_hint: str | None = None,
    ) -> dict[str, object]:
        work_area = self.agent_workspace_service.describe_layout(
            run_id=run_id,
            agent_role=agent.role.value,
            memory_namespace=agent.memory_namespace,
            context_namespace=context_namespace,
            branch_key=branch_key,
            promotion_target_hint=promotion_target_hint,
        )
        self.artifact_lineage_service.record_work_area_scope(
            run_id=run_id,
            proposal_id=proposal_id,
            agent_role=agent.role.value,
            context_namespace=context_namespace,
            artifact_path=work_area["scratch_root"],
            details={
                "branch_key": branch_key,
                "reports_root": work_area["reports_root"],
                "promotion_root": work_area["promotion_root"],
                "shared_workspace_root": work_area["shared_workspace_root"],
                "promotion_policy": work_area["promotion_policy"],
            },
        )
        return {"agent_work_area": work_area}

    def _complete_role_nodes(
        self,
        nodes,
        *,
        role: AgentRole,
        agent_run_id: str,
        handoff_id: str,
        provider_name: str | None,
        details: dict,
    ) -> None:
        for node in nodes:
            if node.role != role:
                continue
            self.agent_service.complete_task_node(
                node.id,
                status="completed",
                details=self._sanitize_task_node_details(details),
                provider_name=provider_name,
                agent_run_id=agent_run_id,
                handoff_id=handoff_id,
            )

    @staticmethod
    def _proposal_parent_node(connector: str, nodes, fallback_id: str) -> str:
        connector_map = {
            "filesystem": "filesystem",
            "http": "http",
            "task": "local",
            "system": "bounded",
        }
        needle = connector_map.get(connector, connector).lower()
        for node in nodes:
            if node.role == AgentRole.REVIEWER and node.node_type == "review" and needle in node.title.lower():
                return node.id
        for node in nodes:
            if node.role == AgentRole.PLANNER:
                return node.id
        return fallback_id

    def _sanitize_agent_payload(self, payload: dict, *, object_type: str) -> dict:
        return self.data_governance_service.sanitize_for_history(payload, object_type=object_type)

    def _sanitize_handoff_payload(self, payload: dict) -> dict:
        return self.data_governance_service.sanitize_for_history(payload, object_type="handoff_payload")

    def _sanitize_task_node_details(self, details: dict) -> dict:
        return self.data_governance_service.sanitize_for_history(details, object_type="task_node_details")
