from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from app.agents.service import AgentService
from app.core.database import Database
from app.core.errors import CancellationRequestedError, SecurityRefusalError
from app.core.utils import json_dumps, json_loads, utcnow_iso
from app.schemas.actions import DataClassification, ProposalStatus
from app.schemas.agents import AgentRole
from app.services.execution_queue_service import ExecutionQueueService
from app.services.graph_node_queue_service import GraphNodeQueueService
from app.services.proposal_service import ProposalService


class GraphRuntimeService:
    NON_EXECUTOR_TERMINAL = {"completed", "failed", "blocked", "cancelled"}
    DEPENDENCY_READY = {"completed", "executed"}
    DEPENDENCY_BLOCKING = {"failed", "blocked", "cancelled"}

    def __init__(
        self,
        database: Database,
        proposal_service: ProposalService,
        queue_service: ExecutionQueueService,
        graph_node_queue_service: GraphNodeQueueService,
        agent_service: AgentService,
    ):
        self.database = database
        self.proposal_service = proposal_service
        self.queue_service = queue_service
        self.graph_node_queue_service = graph_node_queue_service
        self.agent_service = agent_service
        self.planner = None

    def attach_planner(self, planner) -> None:
        self.planner = planner

    def register_run(
        self,
        *,
        run_id: str,
        request_payload: dict,
        summary_id: str | None,
        planner_run_id: str | None,
        planner_handoff_id: str | None,
        correlation_id: str | None,
        initial_state: dict | None = None,
    ) -> None:
        now = utcnow_iso()
        row = self.database.fetch_one("SELECT run_id FROM graph_runs WHERE run_id = ?", (run_id,))
        state_json = json_dumps(initial_state or {})
        if row is None:
            self.database.execute(
                """
                INSERT INTO graph_runs(
                    run_id, request_json, summary_id, planner_run_id, planner_handoff_id,
                    correlation_id, status, state_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    json_dumps(request_payload),
                    summary_id,
                    planner_run_id,
                    planner_handoff_id,
                    correlation_id,
                    "accepted",
                    state_json,
                    now,
                    now,
                ),
            )
            return
        self._update_graph_run(
            run_id,
            request_payload=request_payload,
            summary_id=summary_id,
            planner_run_id=planner_run_id,
            planner_handoff_id=planner_handoff_id,
            correlation_id=correlation_id,
            state_updates=initial_state or {},
            status="running",
        )

    def get_run_context(self, run_id: str) -> dict | None:
        row = self.database.fetch_one("SELECT * FROM graph_runs WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "request": json_loads(row["request_json"], {}),
            "summary_id": row["summary_id"],
            "planner_run_id": row["planner_run_id"],
            "planner_handoff_id": row["planner_handoff_id"],
            "reviewer_run_id": row["reviewer_run_id"],
            "reviewer_handoff_id": row["reviewer_handoff_id"],
            "reporter_run_id": row["reporter_run_id"],
            "reporter_handoff_id": row["reporter_handoff_id"],
            "correlation_id": row["correlation_id"],
            "status": row["status"],
            "state": json_loads(row["state_json"], {}),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    def reconcile_all(self) -> None:
        rows = self.database.fetch_all("SELECT DISTINCT run_id FROM task_nodes ORDER BY created_at ASC")
        for row in rows:
            self.reconcile_run(row["run_id"])

    def resume_all(self, max_steps: int = 100, execute_inline: bool | None = None) -> None:
        rows = self.database.fetch_all(
            "SELECT run_id FROM graph_runs WHERE status NOT IN ('completed', 'failed', 'cancelled') ORDER BY created_at ASC"
        )
        for row in rows:
            self.advance_run(row["run_id"], max_steps=max_steps, execute_inline=execute_inline)

    def advance_run(self, run_id: str, *, max_steps: int = 100, execute_inline: bool | None = None) -> dict | None:
        if self.planner is None:
            raise RuntimeError("Runtime planner is not attached.")
        execute_inline = self._resolve_execute_inline(execute_inline)
        context = self.get_run_context(run_id)
        if context is None:
            return None
        for _ in range(max_steps):
            self.reconcile_run(run_id)
            transitioned = self._ensure_phase_transitions(run_id)
            queued = self._enqueue_ready_non_executor_nodes(run_id)
            executed = False
            if execute_inline:
                executed = self.run_next_non_executor_job(worker_id="graph-inline", run_id=run_id) is not None
            if not (transitioned or queued or executed):
                break
        self.reconcile_run(run_id)
        self._ensure_phase_transitions(run_id)
        self._update_graph_status(run_id)
        return self.get_run_context(run_id)

    def request_retry(self, task_node_id: str, *, actor: str, reason: str) -> None:
        node = self._get_task_node(task_node_id)
        if node.role == AgentRole.EXECUTOR:
            raise ValueError("Use execution retry for executor nodes.")
        affected_nodes = [node] + self._dependent_nodes(node.run_id, node.id)
        self.agent_service.update_task_node(
            node.id,
            status="ready",
            details={"retry_requested_by": actor, "retry_reason": reason, "last_error": None},
            clear_completion=True,
        )
        for dependent in affected_nodes:
            self._clear_node_runtime_handles(dependent.id)
            self._reset_graph_job_for_retry(dependent.id)
        for dependent in affected_nodes[1:]:
            if dependent.role == AgentRole.EXECUTOR:
                continue
            self.agent_service.update_task_node(
                dependent.id,
                status="blocked",
                details={"retry_reset_by": node.id},
                clear_completion=True,
            )
        self._clear_state_for_subgraph(node.run_id, node.id)
        self._clear_graph_phase_context_for_retry(node.run_id, affected_nodes)
        self._update_graph_status(node.run_id)
        self.advance_run(node.run_id, max_steps=20, execute_inline=False)

    def cancel_node(self, task_node_id: str, *, actor: str, reason: str) -> None:
        node = self._get_task_node(task_node_id)
        if node.role == AgentRole.EXECUTOR:
            raise ValueError("Use execution cancellation for executor nodes.")
        job = self.graph_node_queue_service.get_by_task_node_id(node.id)
        if job is not None and job.status == "running":
            self.graph_node_queue_service.request_cancel(job.id, actor=actor, reason=reason)
            self.agent_service.update_task_node(
                node.id,
                status="running",
                details={
                    "cancel_requested_by": actor,
                    "cancel_reason": reason,
                    "cancel_mode": "cooperative",
                },
                clear_completion=True,
            )
            self._update_graph_status(node.run_id)
            return
        self._cancel_graph_job_if_possible(node.id, reason)
        self.agent_service.update_task_node(
            node.id,
            status="cancelled",
            details={"cancelled_by": actor, "cancel_reason": reason},
            finalize=True,
        )
        for dependent in self._dependent_nodes(node.run_id, node.id):
            if dependent.role == AgentRole.EXECUTOR:
                continue
            self._cancel_graph_job_if_possible(dependent.id, reason)
            self.agent_service.update_task_node(
                dependent.id,
                status="blocked",
                details={"blocked_by_cancelled_dependency": node.id, "cancel_reason": reason},
                finalize=True,
            )
        self._update_graph_status(node.run_id)

    def reconcile_run(self, run_id: str) -> None:
        self._reconcile_non_executor_nodes(run_id)
        proposals = [proposal for proposal in self.proposal_service.list() if proposal.run_id == run_id]
        for proposal in proposals:
            self.sync_proposal_lifecycle(proposal.id)
        self._reconcile_non_executor_nodes(run_id)
        self._update_graph_status(run_id)

    def sync_proposal_lifecycle(
        self,
        proposal_id: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> None:
        proposal = self.proposal_service.get(proposal_id)
        job = self.queue_service.get_by_proposal_id(proposal_id)
        proposal_status, proposal_details = self._proposal_node_view(proposal, job, actor=actor, reason=reason)
        executor_status, executor_details = self._executor_node_view(proposal, job, actor=actor, reason=reason)
        self.agent_service.update_nodes_for_proposal(
            proposal_id,
            role=AgentRole.PLANNER,
            status=proposal_status,
            details=proposal_details,
        )
        self.agent_service.update_nodes_for_proposal(
            proposal_id,
            role=AgentRole.EXECUTOR,
            status=executor_status,
            details=executor_details,
        )

    def run_next_non_executor_job(
        self,
        *,
        worker_id: str,
        run_id: str | None = None,
        raise_fail_closed: bool | None = None,
    ) -> dict | None:
        if self.planner is None:
            raise RuntimeError("Runtime planner is not attached.")
        settings = self.planner.base_settings
        if raise_fail_closed is None:
            raise_fail_closed = worker_id == "graph-inline"
        job = self.graph_node_queue_service.claim_next_job(
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
            run_id=run_id,
        )
        if job is None:
            return None
        node = self._get_task_node(job.task_node_id)
        self.agent_service.update_task_node(
            node.id,
            status="running",
            details={"graph_job_id": job.id, "graph_worker_id": worker_id},
            clear_completion=True,
        )
        self.graph_node_queue_service.heartbeat(job.id, worker_id, settings.worker_lease_seconds)
        stop_heartbeat, heartbeat_thread, cancel_check = self._start_non_executor_watchdog(
            job_id=job.id,
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
        )
        try:
            result = self._execute_non_executor_node(
                node.run_id,
                node,
                raise_on_error=True,
                heartbeat_callback=lambda: self.graph_node_queue_service.heartbeat(
                    job.id,
                    worker_id,
                    settings.worker_lease_seconds,
                ),
                cancel_check=cancel_check,
            )
            sanitized_result = self._sanitize_graph_result(result, node=node)
            self.graph_node_queue_service.mark_finished(job.id, status="completed", result=sanitized_result)
            self.advance_run(node.run_id, max_steps=20, execute_inline=False)
            return sanitized_result
        except CancellationRequestedError as exc:
            current_job = self.graph_node_queue_service.get(job.id)
            self.graph_node_queue_service.mark_finished(job.id, status="cancelled", error_text=str(exc))
            self._apply_cancelled_subtree(
                node,
                actor=current_job.cancel_requested_by or worker_id,
                reason=current_job.cancel_reason or str(exc),
            )
            self.planner.audit_service.emit(
                "graph.node_cancelled",
                {
                    "run_id": node.run_id,
                    "task_node_id": node.id,
                    "job_id": job.id,
                    "actor": current_job.cancel_requested_by or worker_id,
                    "reason": current_job.cancel_reason or str(exc),
                    "correlation_id": job.correlation_id,
                },
            )
            self.advance_run(node.run_id, max_steps=20, execute_inline=False)
            return {"status": "cancelled", "error": str(exc), "task_node_id": node.id}
        except Exception as exc:
            self.graph_node_queue_service.mark_finished(job.id, status="failed", error_text=str(exc))
            self.planner.audit_service.emit(
                "graph.node_failed",
                {
                    "run_id": node.run_id,
                    "task_node_id": node.id,
                    "job_id": job.id,
                    "worker_id": worker_id,
                    "error": str(exc),
                    "correlation_id": job.correlation_id,
                },
            )
            self.advance_run(node.run_id, max_steps=20, execute_inline=False)
            if raise_fail_closed:
                raise
            return {"status": "failed", "error": str(exc), "task_node_id": node.id}
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1.0)

    def _execute_non_executor_node(
        self,
        run_id: str,
        node,
        *,
        raise_on_error: bool = False,
        heartbeat_callback=None,
        cancel_check=None,
    ) -> dict | None:
        context = self.get_run_context(run_id)
        if context is None:
            return
        state = dict(context["state"])
        settings = self.planner.provider_service.settings_service.get_effective_settings()
        try:
            self._checkpoint_non_executor_job(heartbeat_callback=heartbeat_callback, cancel_check=cancel_check)
            if node.role == AgentRole.SUPERVISOR and node.node_type == "objective":
                result = self.planner.execute_objective_node(
                    node=node,
                    request_payload=context["request"],
                    correlation_id=context["correlation_id"],
                    effective_settings=settings,
                    heartbeat_callback=heartbeat_callback,
                    cancel_check=cancel_check,
                )
                state["intent_summary"] = result.get("intent_summary")
                state["subtasks"] = result.get("subtasks", [])
                state["planner_graph_ready"] = result.get("planner_graph_ready", False)
                self._update_graph_run(
                    run_id,
                    state=state,
                    planner_run_id=result.get("planner_run_id"),
                    planner_handoff_id=result.get("planner_handoff_id"),
                    status="running",
                    last_error=None,
                )
                return result
            if node.role == AgentRole.PLANNER and node.node_type == "summary":
                if context.get("planner_run_id") is None:
                    raise ValueError(f"Planner run for graph {run_id} is not initialized.")
                result = self.planner.execute_summary_node(
                    node=node,
                    request_payload=context["request"],
                    planner_run_id=context["planner_run_id"],
                    correlation_id=context["correlation_id"],
                    effective_settings=settings,
                    heartbeat_callback=heartbeat_callback,
                    cancel_check=cancel_check,
                )
                state["collected_keys"] = result.get("collected_keys", [])
                state["summary_provider_routing"] = result.get("summary_provider_routing", {})
                state["planning_summary_ready"] = True
                self._update_graph_run(
                    run_id,
                    state=state,
                    summary_id=result.get("summary_id"),
                    status="running",
                    last_error=None,
                )
                return result
            if node.role == AgentRole.PLANNER and node.node_type != "proposal":
                result = self.planner.execute_planner_node(
                    node=node,
                    request_payload=context["request"],
                    summary_id=context["summary_id"],
                    planner_run_id=context["planner_run_id"],
                    correlation_id=context["correlation_id"],
                    effective_settings=settings,
                    heartbeat_callback=heartbeat_callback,
                    cancel_check=cancel_check,
                )
                planner_results = dict(state.get("planner_branch_results", {}))
                planner_results[node.id] = self._sanitize_graph_result(result, node=node)
                state["planner_branch_results"] = planner_results
            elif node.role == AgentRole.REVIEWER and node.node_type == "review":
                planner_result = self._materialize_graph_result(
                    (state.get("planner_branch_results") or {}).get(node.depends_on[0], {})
                )
                result = self.planner.execute_review_node(
                    node=node,
                    planner_result=planner_result,
                    reviewer_run_id=context["reviewer_run_id"],
                    correlation_id=context["correlation_id"],
                    effective_settings=settings,
                    heartbeat_callback=heartbeat_callback,
                    cancel_check=cancel_check,
                )
                review_results = dict(state.get("review_branch_results", {}))
                review_results[node.id] = self._sanitize_graph_result(result, node=node)
                state["review_branch_results"] = review_results
            elif node.node_type == "merge":
                reviewed_results = [
                    self._materialize_graph_result((state.get("review_branch_results") or {}).get(dependency_id, {}))
                    for dependency_id in node.depends_on
                    if (state.get("review_branch_results") or {}).get(dependency_id)
                ]
                result = self.planner.execute_merge_node(
                    node=node,
                    request_payload=context["request"],
                    summary_id=context["summary_id"],
                    reviewer_run_id=context["reviewer_run_id"],
                    planner_run_id=context["planner_run_id"],
                    correlation_id=context["correlation_id"],
                    effective_settings=settings,
                    reviewed_results=reviewed_results,
                    existing_proposal_ids=state.get("proposal_ids"),
                    heartbeat_callback=heartbeat_callback,
                    cancel_check=cancel_check,
                )
                state["proposal_ids"] = result.get("proposal_ids", [])
                state["merge_result"] = self._sanitize_graph_result(result, node=node)
            elif node.role == AgentRole.REPORTER:
                result = self.planner.execute_reporter_node(
                    node=node,
                    request_payload=context["request"],
                    proposal_ids=state.get("proposal_ids", []),
                    reporter_run_id=context["reporter_run_id"],
                    correlation_id=context["correlation_id"],
                    effective_settings=settings,
                    heartbeat_callback=heartbeat_callback,
                    cancel_check=cancel_check,
                )
                sanitized_result = self._sanitize_graph_result(result, node=node)
                state["report_result"] = sanitized_result
                state["operator_summary"] = sanitized_result.get("operator_summary")
            else:
                return
            self._update_graph_run(run_id, state=state, status="running", last_error=None)
            return state.get("report_result") if node.role == AgentRole.REPORTER else result
        except Exception as exc:
            self._update_graph_run(run_id, state=state, status="running", last_error=str(exc))
            if raise_on_error:
                raise
            return {"status": "failed", "error": str(exc)}

    def _ensure_phase_transitions(self, run_id: str) -> bool:
        context = self.get_run_context(run_id)
        if context is None:
            return False
        changed = False
        nodes = self.agent_service.list_task_nodes(run_id)
        planner_nodes = [node for node in nodes if node.role == AgentRole.PLANNER and node.node_type != "proposal"]
        merge_node = next((node for node in nodes if node.node_type == "merge"), None)
        reporter_nodes = [node for node in nodes if node.role == AgentRole.REPORTER]
        if context["state"].get("planner_graph_ready") and ((planner_nodes and self._all_terminal(planner_nodes)) or not planner_nodes):
            changed = self._complete_planner_phase(context) or changed
            context = self.get_run_context(run_id) or context
            if context.get("reviewer_run_id") is None:
                reviewer_agent = self.agent_service.get_by_role(AgentRole.REVIEWER)
                handoff = self.agent_service.create_handoff(
                    run_id,
                    from_agent_run_id=context["planner_run_id"],
                    to_agent=reviewer_agent,
                    title="Candidate actions ready for policy and egress review",
                    payload=self.planner._sanitize_handoff_payload(
                        {"summary_id": context["summary_id"], "branch_count": len(planner_nodes)}
                    ),
                    correlation_id=context["correlation_id"],
                )
                run = self.agent_service.start_run(
                    run_id,
                    reviewer_agent,
                    input_payload=self.planner._sanitize_agent_payload(
                        {"summary_id": context["summary_id"], "branch_count": len(planner_nodes)},
                        object_type="agent_input",
                    ),
                    parent_agent_run_id=context["planner_run_id"],
                    provider_profile=self.planner._provider_profile_for_role(
                        AgentRole.REVIEWER,
                        self.planner.provider_service.settings_service.get_effective_settings(),
                    ),
                    correlation_id=context["correlation_id"],
                )
                self._update_graph_run(
                    run_id,
                    reviewer_run_id=run.id,
                    reviewer_handoff_id=handoff.id,
                    status="running",
                )
                changed = True
                context = self.get_run_context(run_id) or context
        if merge_node is not None and merge_node.status == "completed":
            changed = self._complete_reviewer_phase(context) or changed
            context = self.get_run_context(run_id) or context
            if context.get("reporter_run_id") is None:
                reporter_agent = self.agent_service.get_by_role(AgentRole.REPORTER)
                handoff = self.agent_service.create_handoff(
                    run_id,
                    from_agent_run_id=context["reviewer_run_id"],
                    to_agent=reporter_agent,
                    title="Reviewed plan ready for operator summary",
                    payload=self.planner._sanitize_handoff_payload(
                        {"proposal_ids": context["state"].get("proposal_ids", [])}
                    ),
                    correlation_id=context["correlation_id"],
                )
                run = self.agent_service.start_run(
                    run_id,
                    reporter_agent,
                    input_payload=self.planner._sanitize_agent_payload(
                        {
                            "summary_id": context["summary_id"],
                            "proposal_count": len(context["state"].get("proposal_ids", [])),
                        },
                        object_type="agent_input",
                    ),
                    parent_agent_run_id=context["reviewer_run_id"],
                    provider_profile=self.planner._provider_profile_for_role(
                        AgentRole.REPORTER,
                        self.planner.provider_service.settings_service.get_effective_settings(),
                    ),
                    correlation_id=context["correlation_id"],
                )
                for reporter_node in reporter_nodes:
                    self.agent_service.update_task_node(
                        reporter_node.id,
                        status="ready" if self._dependencies_satisfied(reporter_node, nodes) else reporter_node.status,
                        agent_run_id=run.id,
                        handoff_id=handoff.id,
                        clear_completion=reporter_node.status != "completed",
                    )
                self._update_graph_run(
                    run_id,
                    reporter_run_id=run.id,
                    reporter_handoff_id=handoff.id,
                    status="running",
                )
                changed = True
                context = self.get_run_context(run_id) or context
        if context.get("reporter_run_id"):
            reporter_run = next(
                (record for record in self.agent_service.list_run_history(run_id) if record.id == context["reporter_run_id"]),
                None,
            )
            if reporter_run and reporter_run.status == "completed" and not context["state"].get("planning_completed"):
                self.planner.audit_service.emit(
                    "planning.completed",
                    {
                        "run_id": run_id,
                        "correlation_id": context["correlation_id"],
                        "objective": context["request"]["objective"],
                        "proposal_ids": context["state"].get("proposal_ids", []),
                        "summary_id": context["summary_id"],
                    },
                )
                state = dict(context["state"])
                state["planning_completed"] = True
                self._update_graph_run(run_id, state=state)
                changed = True
        return changed

    def _complete_planner_phase(self, context: dict) -> bool:
        if context.get("planner_run_id") is None:
            return False
        run = next(
            (record for record in self.agent_service.list_run_history(context["run_id"]) if record.id == context["planner_run_id"]),
            None,
        )
        if run is None or run.status != "running":
            return False
        planner_results = (context["state"].get("planner_branch_results") or {}).values()
        proposal_titles = [title for result in planner_results for title in result.get("proposal_titles", [])]
        self.agent_service.complete_handoff(context["planner_handoff_id"])
        self.agent_service.complete_run(
            context["planner_run_id"],
            status="completed",
            provider_name=self._primary_provider_name(context["state"].get("planner_branch_results", {})),
            output_payload=self.planner._sanitize_agent_payload(
                {
                    "summary_id": context["summary_id"],
                    "collected_keys": context["state"].get("collected_keys", []),
                    "proposal_titles": proposal_titles,
                    "proposal_count": len(proposal_titles),
                    "provider_routing": context["state"].get("summary_provider_routing", {}),
                    "branch_count": len(context["state"].get("planner_branch_results", {})),
                },
                object_type="agent_output",
            ),
        )
        return True

    def _complete_reviewer_phase(self, context: dict) -> bool:
        reviewer_run_id = context.get("reviewer_run_id")
        if reviewer_run_id is None:
            return False
        run = next(
            (record for record in self.agent_service.list_run_history(context["run_id"]) if record.id == reviewer_run_id),
            None,
        )
        if run is None or run.status != "running":
            return False
        proposal_ids = context["state"].get("proposal_ids", [])
        if context.get("reviewer_handoff_id"):
            self.agent_service.complete_handoff(context["reviewer_handoff_id"])
        created = [self.proposal_service.get(proposal_id) for proposal_id in proposal_ids]
        self.agent_service.complete_run(
            reviewer_run_id,
            status="completed",
            output_payload=self.planner._sanitize_agent_payload(
                {
                    "proposal_ids": proposal_ids,
                    "blocked_count": sum(1 for proposal in created if proposal.status.value == "blocked"),
                    "approval_required_count": sum(1 for proposal in created if proposal.requires_approval),
                },
                object_type="agent_output",
            ),
        )
        return True

    def _recover_running_non_executor_nodes(self, run_id: str) -> bool:
        changed = False
        runs = {record.id: record for record in self.agent_service.list_run_history(run_id)}
        for node in self.agent_service.list_task_nodes(run_id):
            if node.role == AgentRole.EXECUTOR or (node.proposal_id and node.node_type == "proposal"):
                continue
            if node.status != "running":
                continue
            if node.agent_run_id and node.agent_run_id in runs and runs[node.agent_run_id].status == "running":
                self.agent_service.complete_run(
                    node.agent_run_id,
                    status="failed",
                    output_payload={"recovered_after_restart": True, "reason": "runtime-restart"},
                    provider_name=runs[node.agent_run_id].provider_name,
                )
            self.agent_service.update_task_node(
                node.id,
                status="ready",
                details={"recovered_after_restart": True},
                clear_completion=True,
            )
            changed = True
        return changed

    def _update_graph_status(self, run_id: str) -> None:
        context = self.get_run_context(run_id)
        if context is None:
            return
        nodes = self.agent_service.list_task_nodes(run_id)
        non_executor_nodes = [
            node for node in nodes if node.role != AgentRole.EXECUTOR and not (node.proposal_id and node.node_type == "proposal")
        ]
        proposal_nodes = [node for node in nodes if node.proposal_id and node.node_type == "proposal"]
        executor_nodes = [node for node in nodes if node.role == AgentRole.EXECUTOR]
        status = "running"
        if any(node.status == "failed" for node in non_executor_nodes):
            status = "failed"
        elif any(node.status == "cancelled" for node in non_executor_nodes):
            status = "cancelled"
        elif proposal_nodes and any(node.status == "waiting_approval" for node in proposal_nodes):
            status = "waiting_approval"
        elif executor_nodes and any(node.status in {"ready", "queued", "running"} for node in executor_nodes):
            status = "running"
        elif executor_nodes and all(node.status in {"executed", "failed", "blocked", "cancelled"} for node in executor_nodes):
            status = "completed"
        elif non_executor_nodes and all(node.status in self.NON_EXECUTOR_TERMINAL for node in non_executor_nodes):
            status = "completed"
        self._update_graph_run(
            run_id,
            status=status,
            completed_at=utcnow_iso() if status in {"completed", "failed", "cancelled"} else None,
        )

    def _reconcile_non_executor_nodes(self, run_id: str) -> None:
        nodes = self.agent_service.list_task_nodes(run_id)
        if not nodes:
            return
        node_lookup = {node.id: node for node in nodes}
        agent_runs = {record.id: record for record in self.agent_service.list_run_history(run_id)}
        handoffs = {record.id: record for record in self.agent_service.list_handoffs(run_id)}
        graph_jobs = {record.task_node_id: record for record in self.graph_node_queue_service.list_for_run(run_id)}
        for _ in range(3):
            for node in sorted(nodes, key=self._reconcile_order):
                current = node_lookup[node.id]
                if current.role == AgentRole.EXECUTOR or (current.proposal_id and current.node_type == "proposal"):
                    continue
                status, details, provider_name, finalize = self._non_executor_node_view(
                    current,
                    node_lookup=node_lookup,
                    agent_runs=agent_runs,
                    handoffs=handoffs,
                    graph_job=graph_jobs.get(current.id),
                )
                updated = self.agent_service.update_task_node(
                    current.id,
                    status=status,
                    details=details,
                    provider_name=provider_name,
                    agent_run_id=current.agent_run_id,
                    handoff_id=current.handoff_id,
                    finalize=finalize,
                    clear_completion=status in {"ready", "running", "blocked"},
                )
                node_lookup[current.id] = updated

    def _non_executor_node_view(self, node, *, node_lookup: dict, agent_runs: dict, handoffs: dict, graph_job):
        dependency_states = {
            dependency_id: node_lookup[dependency_id].status
            for dependency_id in node.depends_on
            if dependency_id in node_lookup
        }
        details = {"dependency_states": dependency_states}
        if node.handoff_id and node.handoff_id in handoffs:
            details["handoff_status"] = handoffs[node.handoff_id].status
        if graph_job is not None:
            details["graph_job_id"] = graph_job.id
            details["graph_job_status"] = graph_job.status
            details["graph_job_attempt_count"] = graph_job.attempt_count
            details["graph_job_worker_id"] = graph_job.worker_id
            if graph_job.error_text:
                details["graph_job_error"] = graph_job.error_text
        if node.node_type == "merge":
            if dependency_states and any(state in self.DEPENDENCY_BLOCKING for state in dependency_states.values()):
                details["blocked_by_dependencies"] = [
                    dependency_id for dependency_id, state in dependency_states.items() if state in self.DEPENDENCY_BLOCKING
                ]
                return "blocked", details, node.provider_name, False
            if dependency_states and all(state in self.DEPENDENCY_READY for state in dependency_states.values()):
                if graph_job is not None and graph_job.status in {"queued", "running", "failed", "cancelled", "dead_letter"}:
                    return self._map_graph_job_status(graph_job.status), details, node.provider_name, graph_job.status in {
                        "failed",
                        "cancelled",
                        "dead_letter",
                    }
                return ("ready" if node.status != "completed" else "completed"), details, node.provider_name, node.status == "completed"
            return "blocked", details, node.provider_name, False
        if graph_job is not None:
            mapped = self._map_graph_job_status(graph_job.status)
            if mapped in {"queued", "running", "failed", "cancelled"}:
                return mapped, details, node.provider_name, mapped in self.NON_EXECUTOR_TERMINAL
        run = agent_runs.get(node.agent_run_id) if node.agent_run_id else None
        if run is not None:
            status = self._map_agent_run_status(run.status)
            details["agent_run_status"] = run.status
            if run.output:
                details["agent_output_summary"] = (
                    run.output.get("operator_summary")
                    or run.output.get("intent_summary")
                    or run.output.get("error")
                    or run.output.get("result")
                )
            return status, details, run.provider_name or node.provider_name, status in self.NON_EXECUTOR_TERMINAL
        if node.status == "completed":
            return "completed", details, node.provider_name, True
        if node.status in {"failed", "cancelled"}:
            return node.status, details, node.provider_name, True
        if dependency_states and any(state in self.DEPENDENCY_BLOCKING for state in dependency_states.values()):
            details["blocked_by_dependencies"] = [
                dependency_id for dependency_id, state in dependency_states.items() if state in self.DEPENDENCY_BLOCKING
            ]
            return "blocked", details, node.provider_name, False
        if dependency_states and all(state in self.DEPENDENCY_READY for state in dependency_states.values()):
            return "ready", details, node.provider_name, False
        if dependency_states:
            return "blocked", details, node.provider_name, False
        return node.status, details, node.provider_name, node.status in self.NON_EXECUTOR_TERMINAL

    def _enqueue_ready_non_executor_nodes(self, run_id: str) -> bool:
        changed = False
        for node in sorted(self.agent_service.list_task_nodes(run_id), key=self._reconcile_order):
            if node.role == AgentRole.EXECUTOR or (node.proposal_id and node.node_type == "proposal"):
                continue
            if node.status != "ready":
                continue
            existing = self.graph_node_queue_service.get_by_task_node_id(node.id)
            if existing and existing.status in {"queued", "running"}:
                continue
            self.graph_node_queue_service.enqueue(
                task_node_id=node.id,
                run_id=node.run_id,
                role=node.role,
                node_type=node.node_type,
                queued_by="graph-runtime",
                correlation_id=node.correlation_id,
            )
            self.agent_service.update_task_node(
                node.id,
                status="queued",
                details={"queue_backed": True},
                clear_completion=True,
            )
            changed = True
        return changed

    def _next_ready_non_executor_node(self, run_id: str):
        for node in sorted(self.agent_service.list_task_nodes(run_id), key=self._reconcile_order):
            if node.role == AgentRole.EXECUTOR or (node.proposal_id and node.node_type == "proposal"):
                continue
            if node.status == "ready":
                return node
        return None

    def _resolve_execute_inline(self, execute_inline: bool | None) -> bool:
        if execute_inline is not None:
            return execute_inline
        if self.planner is None:
            return False
        return getattr(self.planner.base_settings, "graph_execution_mode", "background_preferred") == "inline_compat"

    def _get_task_node(self, task_node_id: str):
        row = self.database.fetch_one("SELECT run_id FROM task_nodes WHERE id = ?", (task_node_id,))
        if row is None:
            raise ValueError(f"Task node {task_node_id} was not found.")
        return next(node for node in self.agent_service.list_task_nodes(row["run_id"]) if node.id == task_node_id)

    def _dependent_nodes(self, run_id: str, task_node_id: str) -> list:
        nodes = self.agent_service.list_task_nodes(run_id)
        dependents = []
        frontier = [task_node_id]
        seen = {task_node_id}
        while frontier:
            current = frontier.pop(0)
            for node in nodes:
                if current not in node.depends_on or node.id in seen:
                    continue
                dependents.append(node)
                frontier.append(node.id)
                seen.add(node.id)
        return dependents

    def _clear_state_for_subgraph(self, run_id: str, task_node_id: str) -> None:
        context = self.get_run_context(run_id)
        if context is None:
            return
        state = dict(context["state"])
        affected = {task_node_id} | {node.id for node in self._dependent_nodes(run_id, task_node_id)}
        for key in ("planner_branch_results", "review_branch_results"):
            stored = dict(state.get(key, {}))
            for affected_id in affected:
                stored.pop(affected_id, None)
            state[key] = stored
        merge_or_report_nodes = {
            node.id for node in self.agent_service.list_task_nodes(run_id) if node.node_type in {"merge", "report"}
        }
        if affected & merge_or_report_nodes:
            state.pop("proposal_ids", None)
            state.pop("merge_result", None)
            state.pop("report_result", None)
            state.pop("operator_summary", None)
            state.pop("planning_completed", None)
        self._update_graph_run(run_id, state=state)
        self._purge_unreferenced_runtime_blobs(reason=f"graph-subgraph-reset:{task_node_id}")

    def _checkpoint_non_executor_job(self, *, heartbeat_callback=None, cancel_check=None) -> None:
        if heartbeat_callback:
            heartbeat_callback()
        if cancel_check:
            cancel_check()

    def _start_non_executor_watchdog(self, *, job_id: str, worker_id: str, lease_seconds: int):
        stop_event = threading.Event()
        interval_seconds = max(1.0, lease_seconds / 3)

        def cancel_check() -> None:
            current = self.graph_node_queue_service.get(job_id)
            if current.cancel_requested_at:
                raise CancellationRequestedError(
                    current.cancel_reason or "Cooperative cancellation requested for graph node job."
                )

        def heartbeat_loop() -> None:
            while not stop_event.wait(interval_seconds):
                try:
                    current = self.graph_node_queue_service.get(job_id)
                    if current.status != "running" or current.worker_id != worker_id:
                        return
                    self.graph_node_queue_service.heartbeat(job_id, worker_id, lease_seconds)
                except Exception:
                    return

        thread = threading.Thread(
            target=heartbeat_loop,
            name=f"graph-node-heartbeat-{job_id}",
            daemon=True,
        )
        thread.start()
        return stop_event, thread, cancel_check

    def _sanitize_graph_result(self, result: dict | None, *, node) -> dict:
        if not isinstance(result, dict):
            return {}
        sanitized = {
            key: value
            for key, value in result.items()
            if key not in {"proposals", "reviewed_proposals", "operator_summary"}
        }
        proposal_key = None
        if "proposals" in result:
            proposal_key = "proposals"
        elif "reviewed_proposals" in result:
            proposal_key = "reviewed_proposals"
        if proposal_key:
            proposals = result.get(proposal_key) or []
            storage = self._store_graph_result_blob(
                proposals,
                purpose=f"graph-{node.role.value}-{proposal_key}",
            )
            sanitized[f"{proposal_key}_descriptors"] = self._proposal_descriptors(proposals)
            sanitized[f"{proposal_key}_count"] = len(proposals)
            sanitized[f"{proposal_key}_blob_id"] = storage["blob_id"]
            sanitized[f"{proposal_key}_digest"] = storage["digest"]
            sanitized[f"{proposal_key}_storage_mode"] = storage["storage_mode"]
            sanitized[f"{proposal_key}_storage_class"] = storage["storage_class"]
        if "operator_summary" in result:
            sanitized["operator_summary"] = self.planner.data_governance_service.sanitize_for_history(
                {"operator_summary": result.get("operator_summary")},
                object_type="summary_payload",
            ).get("operator_summary")
        return self.planner.data_governance_service.sanitize_for_history(
            sanitized,
            object_type="history_payload",
        )

    def _materialize_graph_result(self, stored_result: dict | None) -> dict:
        if not isinstance(stored_result, dict):
            return {}
        materialized = dict(stored_result)
        for key in ("proposals", "reviewed_proposals"):
            blob_id = stored_result.get(f"{key}_blob_id")
            if not blob_id:
                continue
            digest = stored_result.get(f"{key}_digest")
            materialized[key] = self._load_graph_result_blob(blob_id, expected_digest=digest)
        return materialized

    def _store_graph_result_blob(self, payload: Any, *, purpose: str) -> dict[str, str | None]:
        serialized = json_dumps(payload)
        blob = self.planner.data_governance_service.protected_storage.store_text_blob(
            serialized,
            classification=self._graph_result_storage_classification(payload),
            purpose=purpose,
        )
        return {
            "blob_id": blob["blob_id"],
            "digest": blob["digest"],
            "storage_mode": blob["storage_mode"],
            "storage_class": self._graph_result_storage_classification(payload),
        }

    def _load_graph_result_blob(self, blob_id: str, *, expected_digest: str | None) -> list[dict]:
        text = self.planner.data_governance_service.protected_storage.load_text_blob(
            blob_id,
            expected_digest=expected_digest,
        )
        payload = json_loads(text, [])
        return payload if isinstance(payload, list) else []

    def _collect_referenced_blob_ids(self) -> set[str]:
        referenced: set[str] = set()
        collect = self.planner.data_governance_service.collect_blob_ids

        for row in self.database.fetch_all(
            """
            SELECT summary_text_blob_id, outbound_summary_text_blob_id, collected_json, lineage_json
            FROM summaries
            """
        ):
            if row["summary_text_blob_id"]:
                referenced.add(row["summary_text_blob_id"])
            if row["outbound_summary_text_blob_id"]:
                referenced.add(row["outbound_summary_text_blob_id"])
            referenced.update(collect(json_loads(row["collected_json"], {})))
            referenced.update(collect(json_loads(row["lineage_json"], {})))

        json_sources = (
            ("proposals", "payload_json"),
            ("proposals", "policy_notes_json"),
            ("action_history", "input_json"),
            ("action_history", "output_json"),
            ("action_history", "boundary_metadata_json"),
            ("connector_runs", "input_json"),
            ("connector_runs", "output_json"),
            ("audit_entries", "payload_json"),
            ("agent_runs", "input_json"),
            ("agent_runs", "output_json"),
            ("handoffs", "payload_json"),
            ("task_nodes", "details_json"),
            ("graph_runs", "state_json"),
            ("graph_node_jobs", "result_json"),
        )
        for table_name, column_name in json_sources:
            rows = self.database.fetch_all(
                f"SELECT {column_name} AS payload FROM {table_name} WHERE {column_name} IS NOT NULL"
            )
            for row in rows:
                referenced.update(collect(json_loads(row["payload"], {})))
        return referenced

    def _purge_unreferenced_runtime_blobs(self, *, reason: str) -> int:
        referenced = self._collect_referenced_blob_ids()
        removed = self.planner.data_governance_service.purge_unreferenced_blobs(referenced)
        if removed:
            self.planner.audit_service.emit(
                "storage.blob_gc",
                {
                    "reason": reason,
                    "removed_count": removed,
                    "referenced_blob_count": len(referenced),
                },
            )
        return removed

    @staticmethod
    def _proposal_descriptors(proposals: list[dict]) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for payload in proposals:
            descriptors.append(
                {
                    "title": payload.get("title"),
                    "connector": payload.get("connector"),
                    "action_type": payload.get("action_type"),
                    "risk_level": payload.get("risk_level"),
                    "requires_approval": payload.get("requires_approval"),
                    "data_classification": payload.get("data_classification"),
                }
            )
        return descriptors

    @staticmethod
    def _graph_result_storage_classification(payload: Any) -> str:
        values: list[str] = []
        if isinstance(payload, list):
            values = [str(item.get("data_classification") or DataClassification.LOCAL_ONLY.value) for item in payload if isinstance(item, dict)]
        if any(value == DataClassification.RESTRICTED.value for value in values):
            return "privileged-sensitive"
        if any(value == DataClassification.LOCAL_ONLY.value for value in values):
            return "sensitive-local"
        return "non-sensitive"

    def _apply_cancelled_subtree(self, node, *, actor: str, reason: str) -> None:
        self.agent_service.update_task_node(
            node.id,
            status="cancelled",
            details={"cancelled_by": actor, "cancel_reason": reason, "cancel_mode": "cooperative"},
            finalize=True,
        )
        for dependent in self._dependent_nodes(node.run_id, node.id):
            if dependent.role == AgentRole.EXECUTOR:
                continue
            self._cancel_graph_job_if_possible(dependent.id, reason)
            self.agent_service.update_task_node(
                dependent.id,
                status="blocked",
                details={"blocked_by_cancelled_dependency": node.id, "cancel_reason": reason},
                finalize=True,
            )

    @staticmethod
    def _is_fail_closed_refusal(exc: Exception) -> bool:
        return isinstance(exc, SecurityRefusalError)

    def _clear_node_runtime_handles(self, task_node_id: str) -> None:
        self.database.execute(
            """
            UPDATE task_nodes
            SET agent_run_id = NULL,
                handoff_id = NULL,
                completed_at = NULL
            WHERE id = ?
            """,
            (task_node_id,),
        )

    def _reset_graph_job_for_retry(self, task_node_id: str) -> None:
        self.database.execute("DELETE FROM graph_node_jobs WHERE task_node_id = ?", (task_node_id,))

    def _cancel_graph_job_if_possible(self, task_node_id: str, reason: str | None) -> None:
        job = self.graph_node_queue_service.get_by_task_node_id(task_node_id)
        if job is None:
            return
        if job.status in {"queued", "failed", "blocked", "dead_letter"}:
            self.graph_node_queue_service.cancel(job.id, reason=reason)

    def _clear_graph_phase_context_for_retry(self, run_id: str, affected_nodes: list) -> None:
        affected_roles = {node.role for node in affected_nodes if node.role != AgentRole.EXECUTOR}
        updates: dict[str, object] = {"status": "running", "completed_at": None}
        if AgentRole.PLANNER in affected_roles or AgentRole.REVIEWER in affected_roles:
            updates.update(
                {
                    "reviewer_run_id": None,
                    "reviewer_handoff_id": None,
                    "reporter_run_id": None,
                    "reporter_handoff_id": None,
                }
            )
        elif AgentRole.REPORTER in affected_roles:
            updates.update({"reporter_run_id": None, "reporter_handoff_id": None})
        self._update_graph_run(run_id, **updates)

    def _dependencies_satisfied(self, node, nodes: list) -> bool:
        lookup = {record.id: record for record in nodes}
        return all(
            lookup[dependency_id].status in self.DEPENDENCY_READY
            for dependency_id in node.depends_on
            if dependency_id in lookup
        )

    @staticmethod
    def _all_terminal(nodes: list) -> bool:
        return all(node.status in {"completed", "failed", "blocked", "cancelled"} for node in nodes)

    @staticmethod
    def _primary_provider_name(results: dict[str, dict]) -> str | None:
        for result in results.values():
            if result.get("provider_name"):
                return result["provider_name"]
        return None

    def _update_graph_run(
        self,
        run_id: str,
        *,
        request_payload: dict | None = None,
        summary_id: str | None = None,
        planner_run_id: str | None = None,
        planner_handoff_id: str | None = None,
        reviewer_run_id: str | None = None,
        reviewer_handoff_id: str | None = None,
        reporter_run_id: str | None = None,
        reporter_handoff_id: str | None = None,
        correlation_id: str | None = None,
        status: str | None = None,
        state: dict | None = None,
        state_updates: dict | None = None,
        last_error: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        current = self.get_run_context(run_id)
        if current is None:
            return
        merged_state = dict(state or current["state"])
        if state_updates:
            merged_state.update(state_updates)
        self.database.execute(
            """
            UPDATE graph_runs
            SET request_json = COALESCE(?, request_json),
                summary_id = COALESCE(?, summary_id),
                planner_run_id = COALESCE(?, planner_run_id),
                planner_handoff_id = COALESCE(?, planner_handoff_id),
                reviewer_run_id = COALESCE(?, reviewer_run_id),
                reviewer_handoff_id = COALESCE(?, reviewer_handoff_id),
                reporter_run_id = COALESCE(?, reporter_run_id),
                reporter_handoff_id = COALESCE(?, reporter_handoff_id),
                correlation_id = COALESCE(?, correlation_id),
                status = COALESCE(?, status),
                state_json = ?,
                last_error = COALESCE(?, last_error),
                updated_at = ?,
                completed_at = COALESCE(?, completed_at)
            WHERE run_id = ?
            """,
            (
                json_dumps(request_payload) if request_payload is not None else None,
                summary_id,
                planner_run_id,
                planner_handoff_id,
                reviewer_run_id,
                reviewer_handoff_id,
                reporter_run_id,
                reporter_handoff_id,
                correlation_id,
                status,
                json_dumps(merged_state),
                last_error,
                utcnow_iso(),
                completed_at,
                run_id,
            ),
        )

    @staticmethod
    def _proposal_node_view(proposal, job, *, actor: str | None, reason: str | None) -> tuple[str, dict]:
        status_map = {
            ProposalStatus.PENDING.value: "waiting_approval",
            ProposalStatus.APPROVED.value: "ready",
            ProposalStatus.QUEUED.value: "queued",
            ProposalStatus.RUNNING.value: "running",
            ProposalStatus.EXECUTED.value: "executed",
            ProposalStatus.REJECTED.value: "cancelled",
            ProposalStatus.FAILED.value: "failed",
            ProposalStatus.BLOCKED.value: "blocked",
            ProposalStatus.STALE.value: "waiting_approval",
        }
        return status_map.get(proposal.status.value, proposal.status.value), {
            "proposal_status": proposal.status.value,
            "job_id": job.id if job else None,
            "job_status": job.status.value if job else None,
            "actor": actor,
            "reason": reason,
            "stale_reason": proposal.stale_reason,
        }

    def _executor_node_view(self, proposal, job, *, actor: str | None, reason: str | None) -> tuple[str, dict]:
        if job is None:
            if proposal.status in {ProposalStatus.PENDING, ProposalStatus.STALE}:
                status = "waiting_approval"
            elif proposal.status == ProposalStatus.APPROVED:
                status = "ready"
            elif proposal.status == ProposalStatus.REJECTED:
                status = "cancelled"
            elif proposal.status == ProposalStatus.EXECUTED:
                status = "executed"
            elif proposal.status == ProposalStatus.FAILED:
                status = "failed"
            elif proposal.status == ProposalStatus.BLOCKED:
                status = "blocked"
            else:
                status = "ready"
            return status, {
                "proposal_status": proposal.status.value,
                "actor": actor,
                "reason": reason,
            }
        lease_expired = bool(
            job.status.value == "running"
            and job.lease_expires_at
            and datetime.fromisoformat(job.lease_expires_at) < datetime.now(UTC)
        )
        if lease_expired:
            status = "queued"
        elif job.status.value == "executed":
            status = "executed"
        elif job.status.value == "failed":
            status = "failed"
        elif job.status.value == "blocked":
            status = "blocked"
        elif job.status.value == "cancelled":
            status = "cancelled"
        elif job.status.value == "dead_letter":
            status = "failed"
        else:
            status = job.status.value
        return status, {
            "proposal_status": proposal.status.value,
            "job_id": job.id,
            "job_status": job.status.value,
            "worker_id": job.worker_id,
            "attempt_count": job.attempt_count,
            "manifest_hash": job.manifest_hash,
            "execution_bundle_hash": job.execution_bundle_hash,
            "boundary_mode": job.boundary_mode,
            "lease_expires_at": job.lease_expires_at,
            "lease_expired_recovery": lease_expired,
            "actor": actor,
            "reason": reason,
            "error": job.error_text,
        }

    @staticmethod
    def _reconcile_order(node) -> tuple[int, str]:
        if node.role == AgentRole.SUPERVISOR:
            return (0, node.id)
        if node.role == AgentRole.PLANNER and node.node_type != "proposal":
            return (1, node.id)
        if node.role == AgentRole.REVIEWER and node.node_type == "review":
            return (2, node.id)
        if node.node_type == "merge":
            return (3, node.id)
        if node.role == AgentRole.REPORTER:
            return (4, node.id)
        return (5, node.id)

    @staticmethod
    def _map_agent_run_status(status: str) -> str:
        if status == "completed":
            return "completed"
        if status == "failed":
            return "failed"
        if status == "blocked":
            return "blocked"
        if status == "cancelled":
            return "cancelled"
        if status == "queued":
            return "queued"
        if status == "ready":
            return "ready"
        return "running"

    @staticmethod
    def _map_graph_job_status(status: str) -> str:
        if status in {"completed", "queued", "running", "failed", "cancelled"}:
            return status
        if status == "dead_letter":
            return "failed"
        if status == "blocked":
            return "blocked"
        return "ready"
