from __future__ import annotations

from datetime import UTC, datetime

from app.agents.service import AgentService
from app.core.database import Database
from app.schemas.actions import ProposalStatus
from app.schemas.agents import AgentRole
from app.services.execution_queue_service import ExecutionQueueService
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
        agent_service: AgentService,
    ):
        self.database = database
        self.proposal_service = proposal_service
        self.queue_service = queue_service
        self.agent_service = agent_service

    def reconcile_all(self) -> None:
        rows = self.database.fetch_all("SELECT DISTINCT run_id FROM task_nodes ORDER BY created_at ASC")
        for row in rows:
            self.reconcile_run(row["run_id"])

    def reconcile_run(self, run_id: str) -> None:
        self._reconcile_non_executor_nodes(run_id)
        proposals = [proposal for proposal in self.proposal_service.list() if proposal.run_id == run_id]
        for proposal in proposals:
            self.sync_proposal_lifecycle(proposal.id)
        self._reconcile_non_executor_nodes(run_id)

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

    def _reconcile_non_executor_nodes(self, run_id: str) -> None:
        nodes = self.agent_service.list_task_nodes(run_id)
        if not nodes:
            return
        node_lookup = {node.id: node for node in nodes}
        agent_runs = {record.id: record for record in self.agent_service.list_run_history(run_id)}
        handoffs = {record.id: record for record in self.agent_service.list_handoffs(run_id)}
        ordered_nodes = sorted(nodes, key=self._reconcile_order)
        for _ in range(3):
            for node in ordered_nodes:
                current = node_lookup[node.id]
                if current.role == AgentRole.EXECUTOR:
                    continue
                if current.proposal_id and current.node_type == "proposal":
                    continue
                status, details, provider_name, finalize = self._non_executor_node_view(
                    current,
                    node_lookup=node_lookup,
                    agent_runs=agent_runs,
                    handoffs=handoffs,
                )
                if status is None:
                    continue
                updated = self.agent_service.update_task_node(
                    current.id,
                    status=status,
                    details=details,
                    provider_name=provider_name,
                    agent_run_id=current.agent_run_id,
                    handoff_id=current.handoff_id,
                    finalize=finalize,
                )
                node_lookup[current.id] = updated

    def _non_executor_node_view(
        self,
        node,
        *,
        node_lookup: dict,
        agent_runs: dict,
        handoffs: dict,
    ) -> tuple[str | None, dict, str | None, bool]:
        dependency_states = {
            dependency_id: node_lookup[dependency_id].status
            for dependency_id in node.depends_on
            if dependency_id in node_lookup
        }
        details: dict = {"dependency_states": dependency_states}
        if node.handoff_id and node.handoff_id in handoffs:
            details["handoff_status"] = handoffs[node.handoff_id].status

        if node.node_type == "merge":
            return self._merge_node_view(node, dependency_states)

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
            return "blocked", details, node.provider_name, True
        if dependency_states and all(state in self.DEPENDENCY_READY for state in dependency_states.values()):
            return "ready", details, node.provider_name, False
        if dependency_states:
            return "blocked", details, node.provider_name, False
        return node.status, details, node.provider_name, node.status in self.NON_EXECUTOR_TERMINAL

    def _merge_node_view(self, node, dependency_states: dict[str, str]) -> tuple[str, dict, str | None, bool]:
        details = {"dependency_states": dependency_states, "merge_key": node.merge_key}
        if dependency_states and any(state in self.DEPENDENCY_BLOCKING for state in dependency_states.values()):
            details["blocked_by_dependencies"] = [
                dependency_id for dependency_id, state in dependency_states.items() if state in self.DEPENDENCY_BLOCKING
            ]
            return "blocked", details, node.provider_name, True
        if dependency_states and all(state in self.DEPENDENCY_READY for state in dependency_states.values()):
            return "completed", details, node.provider_name, True
        if any(state == "running" for state in dependency_states.values()):
            return "running", details, node.provider_name, False
        if any(state == "ready" for state in dependency_states.values()):
            return "ready", details, node.provider_name, False
        return "blocked", details, node.provider_name, False

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
