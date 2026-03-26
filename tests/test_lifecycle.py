from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.core.database import Database
from app.core.errors import FailClosedStorageRefusalError
from app.schemas.actions import ExecutionBoundaryMetadata
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
from app.schemas.agents import AgentRole
from app.services import graph_node_queue_service as graph_node_queue_module
from app.services.graph_node_queue_service import GraphNodeQueueService


def _runtime_settings(tmp_path: Path, *, graph_execution_mode: str) -> AppSettings:
    workspace_root = tmp_path / "workspace"
    return AppSettings(
        app_name="Win Agent Runtime Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit" / "audit.jsonl",
        workspace_root=workspace_root,
        allowed_filesystem_roots=str(workspace_root),
        allowed_http_hosts="example.com,127.0.0.1,localhost,testserver",
        trusted_hosts="127.0.0.1,localhost,testserver",
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        runtime_mode="safe",
        graph_execution_mode=graph_execution_mode,
        allow_insecure_local_storage=True,
        session_secret="test-session-secret",
        session_cookie_name="test_session",
    )


def _insert_task_node(
    database: Database,
    *,
    task_node_id: str,
    run_id: str,
    role: AgentRole = AgentRole.PLANNER,
    node_type: str = "plan",
    status: str = "ready",
) -> None:
    database.execute(
        """
        INSERT INTO task_nodes(
            id, run_id, role, node_type, title, details_json, status, depends_on_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_node_id,
            run_id,
            role.value,
            node_type,
            f"{node_type} node",
            "{}",
            status,
            "[]",
            "2026-01-01T00:00:00+00:00",
        ),
    )


def test_graph_node_enqueue_is_idempotent_under_concurrent_duplicate_admission(tmp_path, monkeypatch):
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    task_node_id = "task-node-concurrent"
    run_id = "run-concurrent"
    _insert_task_node(database, task_node_id=task_node_id, run_id=run_id)

    service_a = GraphNodeQueueService(Database(database.db_path))
    service_b = GraphNodeQueueService(Database(database.db_path))
    start = threading.Barrier(2)
    new_id_release = threading.Event()
    new_id_calls = {"count": 0}
    new_id_lock = threading.Lock()
    original_new_id = graph_node_queue_module.new_id
    results = []
    errors = []
    result_lock = threading.Lock()

    def coordinated_new_id(prefix: str) -> str:
        with new_id_lock:
            new_id_calls["count"] += 1
            if new_id_calls["count"] == 2:
                new_id_release.set()
        new_id_release.wait(timeout=0.25)
        return original_new_id(prefix)

    def run_enqueue(service: GraphNodeQueueService, queued_by: str, correlation_id: str) -> None:
        start.wait()
        try:
            record = service.enqueue(
                task_node_id=task_node_id,
                run_id=run_id,
                role=AgentRole.PLANNER,
                node_type="plan",
                queued_by=queued_by,
                correlation_id=correlation_id,
            )
            with result_lock:
                results.append(record)
        except Exception as exc:  # pragma: no cover - exercised by regression before the fix
            with result_lock:
                errors.append(exc)

    monkeypatch.setattr(graph_node_queue_module, "new_id", coordinated_new_id)

    threads = [
        threading.Thread(target=run_enqueue, args=(service_a, "worker-a", "corr-a")),
        threading.Thread(target=run_enqueue, args=(service_b, "worker-b", "corr-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2

    rows = database.fetch_all("SELECT * FROM graph_node_jobs WHERE task_node_id = ?", (task_node_id,))

    assert len(rows) == 1
    assert {record.id for record in results} == {rows[0]["id"]}
    assert rows[0]["status"] == "queued"
    assert rows[0]["worker_id"] is None
    assert rows[0]["attempt_count"] == 0


def test_duplicate_enqueue_keeps_existing_queued_graph_job_unchanged(tmp_path):
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    task_node_id = "task-node-queued"
    run_id = "run-queued"
    service = GraphNodeQueueService(database)
    _insert_task_node(database, task_node_id=task_node_id, run_id=run_id)

    queued = service.enqueue(
        task_node_id=task_node_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        node_type="plan",
        queued_by="original-queue",
        correlation_id="corr-original",
    )
    duplicate = service.enqueue(
        task_node_id=task_node_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        node_type="plan",
        queued_by="duplicate-queue",
        correlation_id="corr-duplicate",
    )

    assert duplicate.id == queued.id
    assert duplicate.status == "queued"
    assert duplicate.queued_by == queued.queued_by
    assert duplicate.queued_at == queued.queued_at
    assert duplicate.correlation_id == queued.correlation_id
    assert duplicate.attempt_count == queued.attempt_count == 0


def test_duplicate_enqueue_keeps_running_graph_job_claim_state(tmp_path):
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    task_node_id = "task-node-running"
    run_id = "run-running"
    service = GraphNodeQueueService(database)
    _insert_task_node(database, task_node_id=task_node_id, run_id=run_id)

    queued = service.enqueue(
        task_node_id=task_node_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        node_type="plan",
        queued_by="original-queue",
        correlation_id="corr-original",
    )
    claimed = service.claim_next_job(
        worker_id="graph-worker-a",
        lease_seconds=30,
        max_attempts=3,
        run_id=run_id,
    )

    assert claimed is not None

    duplicate = service.enqueue(
        task_node_id=task_node_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        node_type="plan",
        queued_by="duplicate-queue",
        correlation_id="corr-duplicate",
    )

    assert duplicate.id == queued.id
    assert duplicate.status == "running"
    assert duplicate.worker_id == claimed.worker_id == "graph-worker-a"
    assert duplicate.started_at == claimed.started_at
    assert duplicate.lease_expires_at == claimed.lease_expires_at
    assert duplicate.attempt_count == claimed.attempt_count == 1
    assert duplicate.queued_by == queued.queued_by
    assert duplicate.queued_at == queued.queued_at
    assert duplicate.correlation_id == queued.correlation_id


def test_terminal_graph_job_requeue_still_resets_execution_state(tmp_path):
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    task_node_id = "task-node-terminal"
    run_id = "run-terminal"
    service = GraphNodeQueueService(database)
    _insert_task_node(database, task_node_id=task_node_id, run_id=run_id)

    queued = service.enqueue(
        task_node_id=task_node_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        node_type="plan",
        queued_by="original-queue",
        correlation_id="corr-original",
    )
    claimed = service.claim_next_job(
        worker_id="graph-worker-a",
        lease_seconds=30,
        max_attempts=3,
        run_id=run_id,
    )

    assert claimed is not None

    service.request_cancel(claimed.id, actor="pytest", reason="operator requested stop")
    service.mark_finished(claimed.id, status="cancelled", error_text="operator cancelled")

    requeued = service.enqueue(
        task_node_id=task_node_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        node_type="plan",
        queued_by="retry-queue",
        correlation_id="corr-retry",
    )

    assert requeued.id == queued.id
    assert requeued.status == "queued"
    assert requeued.queued_by == "retry-queue"
    assert requeued.correlation_id == "corr-retry"
    assert requeued.started_at is None
    assert requeued.finished_at is None
    assert requeued.worker_id is None
    assert requeued.result == {}
    assert requeued.error_text is None
    assert requeued.lease_expires_at is None
    assert requeued.last_heartbeat_at is None
    assert requeued.attempt_count == 0
    assert requeued.cancel_requested_at is None
    assert requeued.cancel_requested_by is None
    assert requeued.cancel_reason is None


def test_graph_run_can_be_created_before_summary_exists_background_mode(tmp_path):
    container = AppContainer(_runtime_settings(tmp_path, graph_execution_mode="background_preferred"))

    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Queue planning before summary creation",
            task_title="Queued planning",
        )
    )

    graph_context = container.graph_runtime.get_run_context(result.run_id)
    assert graph_context is not None
    assert graph_context["summary_id"] is None
    assert container.summary_service.get_by_run_id(result.run_id) is None
    assert result.summary is None
    assert result.planning_status.value in {"planning_queued", "planning_running", "accepted"}
    assert any(job.node_type == "objective" for job in container.graph_node_queue_service.list_for_run(result.run_id))


def test_graph_first_summary_node_can_fail_and_retry_durably(tmp_path, monkeypatch):
    container = AppContainer(_runtime_settings(tmp_path, graph_execution_mode="background_preferred"))
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Retry summary node durably",
            task_title="Retry summary",
        )
    )

    container.worker.run_once()
    summary_node = next(
        node for node in container.agent_service.list_task_nodes(result.run_id) if node.node_type == "summary"
    )
    original_summarize = container.planner._summarize
    call_count = {"count": 0}

    def flaky_summarize(*args, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            raise RuntimeError("summary provider unavailable")
        return original_summarize(*args, **kwargs)

    monkeypatch.setattr(container.planner, "_summarize", flaky_summarize)

    failed = container.worker.run_once()
    assert failed["status"] == "failed"
    assert container.graph_runtime.get_run_context(result.run_id)["summary_id"] is None

    container.graph_runtime.request_retry(summary_node.id, actor="pytest", reason="retry summary node")
    container.worker.run_once()

    graph_context = container.graph_runtime.get_run_context(result.run_id)
    assert graph_context["summary_id"] is not None
    assert container.summary_service.get_by_run_id(result.run_id) is not None


def test_background_resume_does_not_execute_graph_inline_on_startup(tmp_path):
    settings = _runtime_settings(tmp_path, graph_execution_mode="background_preferred")
    container_a = AppContainer(settings)
    result = container_a.runtime_service.run_agent(
        AgentRunRequest(
            objective="Recover queued graph without inline startup drain",
            task_title="Resume background graph",
        )
    )

    assert container_a.summary_service.get_by_run_id(result.run_id) is None

    container_b = AppContainer(settings)

    assert container_b.summary_service.get_by_run_id(result.run_id) is None
    graph_jobs = container_b.graph_node_queue_service.list_for_run(result.run_id)
    assert graph_jobs
    assert all(job.status in {"queued", "running"} for job in graph_jobs)


def test_inline_compat_mode_still_materializes_summary_inline(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Keep inline compatibility for interactive planning",
            task_title="Inline compatibility",
        )
    )

    assert result.planning_status.value == "planning_completed"
    assert result.summary is not None
    assert result.proposals


def test_proposal_lifecycle_from_run_to_worker_execution(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Create a tracked task for the operator",
            task_title="Follow up on operator task",
            task_details="Check pending work",
        )
    )

    proposal = result.proposals[0]
    assert proposal.status == ProposalStatus.PENDING

    queued = container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="Queue for worker"),
    )

    queued_proposal = container.proposal_service.get(proposal.id)
    assert queued_proposal.status == ProposalStatus.QUEUED
    assert queued["job"].status.value == "queued"
    assert container.history_service.list_action_history() == []

    worker_result = container.worker.run_once()

    updated = container.proposal_service.get(proposal.id)
    job_record = container.execution_queue_service.get(queued["job"].id)
    attempts = container.execution_queue_service.list_attempts(queued["job"].id)
    assert updated.status == ProposalStatus.EXECUTED
    assert worker_result["status"] == "open"
    assert job_record.status.value == "executed"
    assert job_record.boundary_mode == "subprocess-json-bundle"
    assert job_record.execution_bundle_hash
    assert job_record.boundary_metadata["environment_scrubbed"] is True
    assert attempts[0].execution_bundle_hash == job_record.execution_bundle_hash
    assert container.proposal_service.list_approvals(proposal.id)[0].decision == "approved"
    assert container.history_service.list_action_history(limit=1)[0].status == "executed"


def test_reject_proposal(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Track a local task", task_title="Reject me")
    )
    proposal = result.proposals[0]

    container.runtime_service.reject(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="No thanks"),
    )

    updated = container.proposal_service.get(proposal.id)
    assert updated.status == ProposalStatus.REJECTED


def test_execution_boundary_narrows_exact_filesystem_scope(container):
    target = container.base_settings.resolved_workspace_root / "boundary.txt"
    target.write_text("boundary", encoding="utf-8")

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Read one local file", filesystem_path="boundary.txt")
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "filesystem.read_text")
    queued = container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="Boundary test"),
    )

    container.worker.run_once()

    job_record = container.execution_queue_service.get(queued["job"].id)
    assert job_record.boundary_metadata["scope_strategy"] == "exact-task-scope"
    assert str(target.resolve()) in job_record.boundary_metadata["granted_file_paths"]
    assert job_record.boundary_metadata["filesystem_scope"] == [str(target.resolve())]


def test_execution_boundary_narrows_http_scope_to_exact_target(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Call the allowlisted HTTP endpoint",
            http_url="https://example.com/api",
            provider_name="mock",
        )
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "http.get")
    approval = container.proposal_service.approve(proposal.id, "pytest", "HTTP boundary")
    runtime_payload = container.data_governance_service.materialize_action_payload(proposal.payload)
    bundle = container.executor.boundary_runner.build_bundle(
        proposal=proposal,
        approval_id=approval.id,
        manifest_hash=approval.manifest_hash or "",
        runtime_payload=runtime_payload,
        allowed_connectors=["http"],
        capabilities=["execute-approved-action"],
        effective_settings=container.settings_service.get_effective_settings(),
    )

    assert bundle.execution_settings["allowed_http_hosts"] == ["example.com"]
    assert bundle.execution_settings["allowed_http_schemes"] == ["https"]
    assert bundle.execution_settings["allowed_http_ports"] == [443]
    assert bundle.boundary.network_scope == ["GET https://example.com/api"]


def test_task_execution_boundary_uses_brokered_database_access(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Boundary broker")
    )
    proposal = result.proposals[0]
    queued = container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="Task broker test"),
    )

    container.worker.run_once()

    job_record = container.execution_queue_service.get(queued["job"].id)
    assert job_record.boundary_metadata["database_access"] == "brokered-task-actions"
    assert job_record.boundary_metadata["backend"] == "subprocess-json-bundle"


def test_worker_heartbeats_during_boundary_execution(container, monkeypatch):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Heartbeat task")
    )
    proposal = result.proposals[0]
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="heartbeat test"),
    )

    heartbeat_calls: list[int] = []
    original_heartbeat = container.execution_queue_service.heartbeat

    def heartbeat_spy(job_id, worker_id, lease_seconds):
        heartbeat_calls.append(1)
        return original_heartbeat(job_id, worker_id, lease_seconds)

    def boundary_execute(bundle, heartbeat_callback=None):
        if heartbeat_callback is not None:
            heartbeat_callback()
            heartbeat_callback()
        return {"task_id": "brokered", "status": "open"}, ExecutionBoundaryMetadata(
            mode="subprocess-json-bundle",
            isolation_level="child-process / env-scrubbed / same-user",
            backend="subprocess-json-bundle",
        )

    monkeypatch.setattr(container.execution_queue_service, "heartbeat", heartbeat_spy)
    monkeypatch.setattr(container.executor.boundary_runner, "execute", boundary_execute)

    container.worker.run_once()

    assert len(heartbeat_calls) >= 3


def test_child_environment_does_not_inherit_pythonpath(container, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "AMBIENT_TEST_PATH")

    env = container.executor.boundary_runner._scrubbed_environment()

    assert "PYTHONPATH" not in env
    assert all(".codex-pkgs" not in value for value in env.values())


def test_child_import_paths_ignore_arbitrary_parent_paths(container, monkeypatch, tmp_path):
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setattr(
        "app.runtime.execution_boundary.sys.path",
        [str(ambient), str(container.base_settings.project_root / ".codex-pkgs")],
    )

    import_paths = container.executor.boundary_runner._child_import_paths()

    assert str(ambient.resolve()) not in import_paths
    assert any(path.endswith(".codex-pkgs") for path in import_paths)


def test_child_launch_command_uses_isolated_python(container):
    command = container.executor.boundary_runner._child_command()

    executable_name = Path(command[0]).name.lower()
    assert executable_name.startswith("python")
    assert "-I" in command
    assert "-S" in command
    assert "app.runtime.child_process" in command[-1]


def test_child_timeout_limits_extend_beyond_worker_lease(container):
    soft_timeout, hard_timeout = container.executor.boundary_runner._child_timeout_limits()

    assert soft_timeout == container.base_settings.worker_lease_seconds
    assert hard_timeout > soft_timeout


def test_run_subprocess_allows_heartbeat_extended_runtime(container, monkeypatch):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Slow child test")
    )
    proposal = result.proposals[0]
    approval = container.proposal_service.approve(proposal.id, "pytest", "Slow child")
    runtime_payload = container.data_governance_service.materialize_action_payload(proposal.payload)
    bundle = container.executor.boundary_runner.build_bundle(
        proposal=proposal,
        approval_id=approval.id,
        manifest_hash=approval.manifest_hash or "",
        runtime_payload=runtime_payload,
        allowed_connectors=["task"],
        capabilities=["execute-approved-action"],
        effective_settings=container.settings_service.get_effective_settings(),
    )

    heartbeat_calls: list[int] = []

    class FakeProcess:
        def __init__(self):
            self.calls = 0
            self.killed = False

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.calls < 3:
                raise subprocess.TimeoutExpired(cmd="python", timeout=timeout)
            return json.dumps({"ok": True, "result": {"status": "open"}}), ""

        def kill(self):
            self.killed = True

    fake_process = FakeProcess()
    monotonic_values = iter([0.0, 20.0, 40.0])

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr("app.runtime.execution_boundary.time.monotonic", lambda: next(monotonic_values))

    result = container.executor.boundary_runner._run_subprocess(
        bundle,
        heartbeat_callback=lambda: heartbeat_calls.append(1),
    )

    assert result["ok"] is True
    assert fake_process.killed is False
    assert len(heartbeat_calls) == 2


def test_executor_node_tracks_approval_queue_and_execution(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Graph lifecycle task")
    )
    proposal = result.proposals[0]

    executor_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert executor_node.status == "waiting_approval"
    assert executor_node.completed_at is None

    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="queue lifecycle"),
    )

    queued_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert queued_node.status == "queued"
    assert queued_node.completed_at is None

    container.worker.run_once()

    executed_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert executed_node.status == "executed"
    assert executed_node.completed_at is not None


def test_cancel_and_retry_execution_updates_graph_nodes(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Cancelable task")
    )
    proposal = result.proposals[0]
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="queue first"),
    )

    cancel_result = container.runtime_service.cancel_execution(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="cancel queued execution"),
    )
    cancelled_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert cancel_result["job"].status.value == "cancelled"
    assert container.proposal_service.get(proposal.id).status == ProposalStatus.APPROVED
    assert cancelled_node.status == "cancelled"

    retry_result = container.runtime_service.retry_execution(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="retry cancelled execution"),
    )
    retried_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert retry_result["job"].status.value == "queued"
    assert retried_node.status == "queued"

    container.worker.run_once()
    executed_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert executed_node.status == "executed"


def test_graph_runtime_recovers_expired_running_job_after_restart(app_settings):
    container_a = AppContainer(app_settings)
    result = container_a.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Recoverable task")
    )
    proposal = result.proposals[0]
    queued = container_a.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="queue for restart"),
    )
    job_id = queued["job"].id

    container_a.execution_queue_service.claim_next_job(
        worker_id="worker-a",
        lease_seconds=1,
        max_attempts=container_a.base_settings.worker_max_attempts,
    )
    container_a.proposal_service.set_execution_status(proposal.id, ProposalStatus.RUNNING)
    container_a.database.execute(
        "UPDATE execution_jobs SET lease_expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", job_id),
    )

    container_b = AppContainer(app_settings)
    recovered_node = next(
        node
        for node in container_b.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert recovered_node.status == "queued"
    assert recovered_node.details["lease_expired_recovery"] is True

    container_b.worker.run_once()
    executed_node = next(
        node
        for node in container_b.agent_service.list_task_nodes(result.run_id)
        if node.proposal_id == proposal.id and node.role == AgentRole.EXECUTOR
    )
    assert executed_node.status == "executed"


def test_graph_runtime_reconciles_reporter_node_after_restart(app_settings):
    container_a = AppContainer(app_settings)
    result = container_a.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Reporter restart task")
    )
    reporter_node = next(
        node
        for node in container_a.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.REPORTER
    )
    container_a.database.execute(
        "UPDATE task_nodes SET status = ?, completed_at = NULL WHERE id = ?",
        ("blocked", reporter_node.id),
    )

    container_b = AppContainer(app_settings)
    recovered = next(
        node
        for node in container_b.agent_service.list_task_nodes(result.run_id)
        if node.id == reporter_node.id
    )

    assert recovered.status == "completed"
    assert recovered.details["agent_run_status"] == "completed"


def test_graph_runtime_reconciles_merge_gate_after_restart(app_settings):
    container_a = AppContainer(app_settings)
    result = container_a.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Merge restart task")
    )
    merge_node = next(
        node
        for node in container_a.agent_service.list_task_nodes(result.run_id)
        if node.node_type == "merge"
    )
    container_a.database.execute(
        "UPDATE task_nodes SET status = ?, completed_at = NULL WHERE id = ?",
        ("running", merge_node.id),
    )

    container_b = AppContainer(app_settings)
    recovered = next(
        node
        for node in container_b.agent_service.list_task_nodes(result.run_id)
        if node.id == merge_node.id
    )

    assert recovered.status == "completed"
    assert all(state == "completed" for state in recovered.details["dependency_states"].values())


def test_agents_get_distinct_scratch_work_areas(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Review a local file and create a task",
            filesystem_path="notes.txt",
            task_title="Scratch separation",
        )
    )

    task_nodes = container.agent_service.list_task_nodes(result.run_id)
    scratch_roots = {
        node.role.value: node.details["agent_work_area"]["scratch_root"]
        for node in task_nodes
        if node.details.get("agent_work_area", {}).get("scratch_root")
    }

    assert scratch_roots["supervisor"] != scratch_roots["planner"]
    assert scratch_roots["planner"] != scratch_roots["reviewer"]
    assert scratch_roots["reviewer"] != scratch_roots["executor"]
    assert all(Path(path).exists() for path in scratch_roots.values())


def test_agent_work_areas_require_explicit_promotion_to_shared_workspace(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a local task", task_title="Promotion policy")
    )

    reporter_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.REPORTER
    )
    work_area = reporter_node.details["agent_work_area"]

    assert work_area["promotion_policy"] == "approved-filesystem-copy-or-move"
    assert Path(work_area["shared_workspace_root"]) == container.base_settings.resolved_workspace_root
    assert Path(work_area["scratch_root"]) != container.base_settings.resolved_workspace_root
    assert "approved filesystem.copy_path or filesystem.move_path action" in work_area["promotion_note"]


def test_execution_boundary_uses_executor_scratch_area_as_child_cwd(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Executor scratch cwd")
    )
    proposal = result.proposals[0]
    approval = container.proposal_service.approve(proposal.id, "pytest", "Executor scratch cwd")
    runtime_payload = container.data_governance_service.materialize_action_payload(proposal.payload)
    bundle = container.executor.boundary_runner.build_bundle(
        proposal=proposal,
        approval_id=approval.id,
        manifest_hash=approval.manifest_hash or "",
        runtime_payload=runtime_payload,
        allowed_connectors=["task"],
        capabilities=["execute-approved-action"],
        effective_settings=container.settings_service.get_effective_settings(),
    )

    assert bundle.execution_settings["child_cwd"] == bundle.boundary.agent_scratch_root
    assert bundle.boundary.shared_workspace_root == str(container.base_settings.resolved_workspace_root)
    assert bundle.boundary.agent_scratch_root is not None
    assert Path(bundle.boundary.agent_scratch_root).exists()


def test_executor_scratch_path_is_distinct_per_proposal_even_for_same_connector(container):
    settings = container.settings_service.get_effective_settings()
    proposal_a = SimpleNamespace(
        id="proposal_a",
        run_id="run_shared_connector_scope",
        connector="task",
        action_type="task.create",
        correlation_id="corr-a",
    )
    proposal_b = SimpleNamespace(
        id="proposal_b",
        run_id="run_shared_connector_scope",
        connector="task",
        action_type="task.create",
        correlation_id="corr-b",
    )

    bundle_a = container.executor.boundary_runner.build_bundle(
        proposal=proposal_a,
        approval_id="approval_a",
        manifest_hash="manifest_a",
        runtime_payload={"title": "A", "details": "alpha"},
        allowed_connectors=["task"],
        capabilities=["execute-approved-action"],
        effective_settings=settings,
    )
    bundle_b = container.executor.boundary_runner.build_bundle(
        proposal=proposal_b,
        approval_id="approval_b",
        manifest_hash="manifest_b",
        runtime_payload={"title": "B", "details": "bravo"},
        allowed_connectors=["task"],
        capabilities=["execute-approved-action"],
        effective_settings=settings,
    )

    assert bundle_a.boundary.agent_scratch_root != bundle_b.boundary.agent_scratch_root
    assert bundle_a.execution_settings["agent_context_namespace"] != bundle_b.execution_settings["agent_context_namespace"]


def test_artifact_lineage_records_promotion_events(container):
    layout = container.agent_workspace_service.layout_for(
        run_id="run_artifact_lineage",
        agent_role="executor",
        memory_namespace="executor",
        context_namespace="executor:run_artifact_lineage:proposal_copy:filesystem",
        branch_key="filesystem",
    )
    source = layout.promotion_root / "draft.txt"
    destination = container.base_settings.resolved_workspace_root / "published.txt"
    source.write_text("draft", encoding="utf-8")

    events = container.artifact_lineage_service.record_execution_artifacts(
        run_id="run_artifact_lineage",
        proposal_id="proposal_copy",
        agent_role="executor",
        context_namespace="executor:run_artifact_lineage:proposal_copy:filesystem",
        action_type="filesystem.copy_path",
        payload={"source_path": str(source), "destination_path": str(destination)},
        result={"source_path": str(source), "destination_path": str(destination), "copied": True},
        scratch_root=str(layout.scratch_root),
        promotion_root=str(layout.promotion_root),
        shared_workspace_root=str(layout.shared_workspace_root),
    )

    assert any(event.event_type == "promotion" for event in events)
    stored = container.artifact_lineage_service.list_events("run_artifact_lineage")
    promotion = next(event for event in stored if event.event_type == "promotion")
    assert promotion.source_path == str(source.resolve())
    assert promotion.destination_path == str(destination.resolve())


def test_agent_workspace_cleanup_skips_active_runs_and_removes_stale_roots(container):
    stale_layout = container.agent_workspace_service.layout_for(
        run_id="run_stale_workspace",
        agent_role="planner",
        memory_namespace="planner",
        context_namespace="planner:run_stale_workspace:stale",
        branch_key="stale",
    )
    active_layout = container.agent_workspace_service.layout_for(
        run_id="run_active_workspace",
        agent_role="planner",
        memory_namespace="planner",
        context_namespace="planner:run_active_workspace:active",
        branch_key="active",
    )
    old_timestamp = 946684800  # 2000-01-01 UTC
    for path in (stale_layout.run_root, active_layout.run_root):
        os.utime(path, (old_timestamp, old_timestamp))

    planner = container.agent_service.get_by_role(AgentRole.PLANNER)
    container.agent_service.create_task_node(
        "run_active_workspace",
        role=AgentRole.PLANNER,
        node_type="plan",
        title="Active planner node",
        details={"agent_work_area": active_layout.as_dict()},
        status="running",
        context_namespace="planner:run_active_workspace:active",
        agent=planner,
        depends_on=[],
    )

    dry_run = container.artifact_lineage_service.cleanup_stale_work_areas(dry_run=True, retention_days=1)
    assert str(stale_layout.run_root) in dry_run["removed"]
    assert str(active_layout.run_root) not in dry_run["removed"]

    result = container.artifact_lineage_service.cleanup_stale_work_areas(dry_run=False, retention_days=1)
    assert str(stale_layout.run_root) in result["removed"]
    assert stale_layout.run_root.exists() is False
    assert active_layout.run_root.exists() is True

def test_filesystem_read_history_redacts_preview(container, monkeypatch):
    target = container.base_settings.resolved_workspace_root / "secret.txt"
    target.write_text("TOP SECRET FILE CONTENT", encoding="utf-8")
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Inspect a local note", filesystem_path="secret.txt")
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "filesystem.read_text")
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="Read for redaction test"),
    )

    monkeypatch.setattr(
        container.executor.boundary_runner,
        "execute",
        lambda bundle, heartbeat_callback=None: (
            {"path": str(target), "preview": "TOP SECRET FILE CONTENT", "size_bytes": len("TOP SECRET FILE CONTENT")},
            ExecutionBoundaryMetadata(
                mode="subprocess-json-bundle",
                isolation_level="child-process / env-scrubbed / same-user",
                backend="subprocess-json-bundle",
            ),
        ),
    )

    container.worker.run_once()

    history = container.history_service.list_action_history(limit=1)[0]
    assert "TOP SECRET FILE CONTENT" not in json.dumps(history.output)
    assert history.output["preview"]["redacted"] is True


def test_http_execution_history_redacts_body_preview(container, monkeypatch):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Fetch an allowlisted endpoint", http_url="https://example.com/api", provider_name="mock")
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "http.get")
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="HTTP redaction test"),
    )

    monkeypatch.setattr(
        container.executor.boundary_runner,
        "execute",
        lambda bundle, heartbeat_callback=None: (
            {
                "url": "https://example.com/api",
                "status_code": 200,
                "headers": {"content-type": "application/json"},
                "body_preview": "{\"token\":\"TOP SECRET BODY\"}",
            },
            ExecutionBoundaryMetadata(
                mode="subprocess-json-bundle",
                isolation_level="child-process / env-scrubbed / same-user",
                backend="subprocess-json-bundle",
            ),
        ),
    )

    container.worker.run_once()

    history = container.history_service.list_action_history(limit=1)[0]
    assert "TOP SECRET BODY" not in json.dumps(history.output)
    assert history.output["body_preview"]["redacted"] is True


def test_system_read_history_redacts_preview(container, monkeypatch):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Read a bounded system file",
            system_action="system.read_text_file",
            system_path="notes.txt",
            provider_name="mock",
        )
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "system.read_text_file")
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="System redaction test"),
    )

    monkeypatch.setattr(
        container.executor.boundary_runner,
        "execute",
        lambda bundle, heartbeat_callback=None: (
            {"path": "notes.txt", "preview": "TOP SECRET SYSTEM CONTENT", "size_bytes": 25},
            ExecutionBoundaryMetadata(
                mode="subprocess-json-bundle",
                isolation_level="child-process / env-scrubbed / same-user",
                backend="subprocess-json-bundle",
            ),
        ),
    )

    container.worker.run_once()

    history = container.history_service.list_action_history(limit=1)[0]
    assert "TOP SECRET SYSTEM CONTENT" not in json.dumps(history.output)
    assert history.output["preview"]["redacted"] is True


def test_graph_runtime_waiting_approval_progresses_to_completed_after_execution(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Durable graph status")
    )

    waiting = container.graph_runtime.get_run_context(result.run_id)
    assert waiting["status"] == "waiting_approval"
    assert waiting["state"]["planning_completed"] is True
    assert waiting["state"]["proposal_ids"]

    proposal = result.proposals[0]
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="finish graph runtime"),
    )
    container.worker.run_once()
    container.graph_runtime.reconcile_run(result.run_id)

    completed = container.graph_runtime.get_run_context(result.run_id)
    assert completed["status"] == "completed"


def test_graph_runtime_resumes_partial_non_executor_run_after_restart(app_settings, monkeypatch):
    container_a = AppContainer(app_settings)
    original_advance = container_a.graph_runtime.advance_run
    monkeypatch.setattr(
        container_a.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=1, execute_inline=execute_inline
        ),
    )

    result = container_a.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Resume non executor runtime")
    )

    partial = container_a.graph_runtime.get_run_context(result.run_id)
    assert partial["status"] == "running"
    assert partial["state"].get("planning_completed") is not True

    container_b = AppContainer(app_settings)
    resumed = container_b.graph_runtime.get_run_context(result.run_id)

    assert resumed["state"]["planning_completed"] is True
    assert resumed["status"] == "waiting_approval"
    assert resumed["state"]["proposal_ids"]


def test_graph_runtime_can_drain_non_executor_queue_via_worker(app_settings, monkeypatch):
    container = AppContainer(app_settings)
    original_advance = container.graph_runtime.advance_run
    monkeypatch.setattr(
        container.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Queued worker drain")
    )
    container.worker.run_once()
    container.worker.run_once()
    queued_nodes = container.agent_service.list_task_nodes(result.run_id)
    planner_node = next(
        node
        for node in queued_nodes
        if node.role == AgentRole.PLANNER and node.node_type not in {"proposal", "summary"}
    )
    queued_job = container.graph_node_queue_service.get_by_task_node_id(planner_node.id)

    assert planner_node.status == "queued"
    assert queued_job is not None
    assert queued_job.status == "queued"

    for _ in range(12):
        if container.worker.run_once() is None:
            break

    drained_nodes = container.agent_service.list_task_nodes(result.run_id)
    drained_planner = next(node for node in drained_nodes if node.id == planner_node.id)
    drained_review = next(
        node
        for node in drained_nodes
        if node.role == AgentRole.REVIEWER and node.node_type == "review"
    )
    resumed = container.graph_runtime.get_run_context(result.run_id)

    assert drained_planner.status == "completed"
    assert drained_review.status == "completed"
    assert resumed["status"] == "waiting_approval"
    assert resumed["state"]["proposal_ids"]


def test_graph_runtime_resumes_queued_non_executor_nodes_after_restart(app_settings, monkeypatch):
    container_a = AppContainer(app_settings)
    original_advance = container_a.graph_runtime.advance_run
    monkeypatch.setattr(
        container_a.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )

    result = container_a.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Queued restart resume")
    )
    container_a.worker.run_once()
    container_a.worker.run_once()
    queued_node = next(
        node
        for node in container_a.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.node_type not in {"proposal", "summary"}
    )
    queued_job = container_a.graph_node_queue_service.get_by_task_node_id(queued_node.id)

    assert queued_node.status == "queued"
    assert queued_job is not None
    assert queued_job.status == "queued"

    container_b = AppContainer(app_settings)
    resumed = container_b.graph_runtime.get_run_context(result.run_id)
    resumed_nodes = container_b.agent_service.list_task_nodes(result.run_id)
    resumed_planner = next(node for node in resumed_nodes if node.id == queued_node.id)

    assert resumed_planner.status == "completed"
    assert resumed["status"] == "waiting_approval"
    assert resumed["state"]["proposal_ids"]


def test_graph_runtime_retry_replays_failed_planner_branch(container, monkeypatch):
    original_build = container.planner._build_branch_proposals
    original_advance = container.graph_runtime.advance_run
    failed_once = {"value": False}

    def flaky_build(*args, **kwargs):
        if kwargs.get("branch_key") == "filesystem" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("planner branch failed once")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        container.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )
    monkeypatch.setattr(container.planner, "_build_branch_proposals", flaky_build)

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Inspect a local note", filesystem_path="notes.txt")
    )
    container.worker.run_once()
    container.worker.run_once()
    container.worker.run_once()
    planner_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.branch_key == "filesystem"
    )
    review_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.REVIEWER and node.node_type == "review"
    )

    assert planner_node.status == "failed"
    assert review_node.status == "blocked"

    container.graph_runtime.request_retry(planner_node.id, actor="pytest", reason="retry planner branch")
    container.graph_runtime.advance_run(result.run_id)
    for _ in range(12):
        if container.worker.run_once() is None:
            break

    retried_nodes = container.agent_service.list_task_nodes(result.run_id)
    retried_planner = next(node for node in retried_nodes if node.id == planner_node.id)
    retried_review = next(node for node in retried_nodes if node.id == review_node.id)
    resumed = container.graph_runtime.get_run_context(result.run_id)

    assert retried_planner.status == "completed"
    assert retried_review.status == "completed"
    assert resumed["state"]["planning_completed"] is True
    assert resumed["state"]["proposal_ids"]


def test_graph_runtime_cancelled_queued_non_executor_job_is_persisted(app_settings, monkeypatch):
    container = AppContainer(app_settings)
    original_advance = container.graph_runtime.advance_run
    monkeypatch.setattr(
        container.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Inspect a local note", filesystem_path="notes.txt")
    )
    container.worker.run_once()
    container.worker.run_once()
    planner_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.branch_key == "filesystem"
    )

    container.graph_runtime.cancel_node(planner_node.id, actor="pytest", reason="cancel queued planner node")

    cancelled_nodes = container.agent_service.list_task_nodes(result.run_id)
    cancelled_planner = next(node for node in cancelled_nodes if node.id == planner_node.id)
    cancelled_job = container.graph_node_queue_service.get_by_task_node_id(planner_node.id)

    assert cancelled_planner.status == "cancelled"
    assert cancelled_job is not None
    assert cancelled_job.status == "cancelled"


def test_graph_runtime_cancelled_non_executor_node_blocks_dependents(app_settings, monkeypatch):
    container = AppContainer(app_settings)
    original_advance = container.graph_runtime.advance_run
    monkeypatch.setattr(
        container.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Inspect a local note", filesystem_path="notes.txt")
    )
    container.worker.run_once()
    container.worker.run_once()
    planner_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.branch_key == "filesystem" and node.node_type != "summary"
    )

    container.graph_runtime.cancel_node(planner_node.id, actor="pytest", reason="cancel branch")

    nodes = container.agent_service.list_task_nodes(result.run_id)
    cancelled_planner = next(node for node in nodes if node.id == planner_node.id)
    blocked_review = next(node for node in nodes if node.role == AgentRole.REVIEWER and node.node_type == "review")
    blocked_merge = next(node for node in nodes if node.node_type == "merge")
    blocked_reporter = next(node for node in nodes if node.role == AgentRole.REPORTER)
    context = container.graph_runtime.get_run_context(result.run_id)

    assert cancelled_planner.status == "cancelled"
    assert blocked_review.status == "blocked"
    assert blocked_merge.status == "blocked"


def test_graph_runtime_state_does_not_persist_filesystem_write_content(container):
    secret = "VERY SECRET FILE CONTENT"
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Write protected file content",
            filesystem_path="secret.txt",
            file_content=secret,
        )
    )
    graph_row = container.database.fetch_one("SELECT state_json FROM graph_runs WHERE run_id = ?", (result.run_id,))
    job_rows = container.database.fetch_all(
        "SELECT result_json FROM graph_node_jobs WHERE run_id = ? ORDER BY queued_at ASC",
        (result.run_id,),
    )

    assert secret not in graph_row["state_json"]
    assert all(secret not in (row["result_json"] or "") for row in job_rows)


def test_reporter_provider_failure_returns_governance_safe_fallback(container, monkeypatch):
    secret = "VERY SECRET OBJECTIVE CONTEXT"
    original_complete = container.provider_service.complete

    def fail_report_provider(request):
        if request.task_type == "report-plan":
            raise RuntimeError("report provider unavailable")
        return original_complete(request)

    monkeypatch.setattr(container.provider_service, "complete", fail_report_provider)

    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective=f"Inspect deferred evidence for {secret}",
            filesystem_path="secret.txt",
        )
    )
    context = container.graph_runtime.get_run_context(result.run_id)

    assert context is not None
    assert "Plan ready." in context["state"]["operator_summary"]
    assert "deferred" in context["state"]["operator_summary"].lower()
    assert secret not in context["state"]["operator_summary"]
    assert "report provider unavailable" not in context["state"]["operator_summary"]


def test_graph_node_job_result_does_not_persist_restricted_http_body(container):
    secret = "{\"token\":\"TOP SECRET HTTP BODY\"}"
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Post to an allowlisted endpoint",
            http_url="https://example.com/api",
            http_method="POST",
            http_body=secret,
        )
    )
    job_rows = container.database.fetch_all(
        "SELECT result_json FROM graph_node_jobs WHERE run_id = ? ORDER BY queued_at ASC",
        (result.run_id,),
    )

    assert job_rows
    assert all(secret not in (row["result_json"] or "") for row in job_rows)


def test_graph_runtime_retry_purges_orphaned_graph_result_blobs_and_preserves_summary_blob(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Write protected file content",
            filesystem_path="secret.txt",
            file_content="VERY SECRET FILE CONTENT",
        )
    )
    state_row = container.database.fetch_one("SELECT state_json FROM graph_runs WHERE run_id = ?", (result.run_id,))
    job_rows = container.database.fetch_all(
        "SELECT result_json FROM graph_node_jobs WHERE run_id = ? ORDER BY queued_at ASC",
        (result.run_id,),
    )
    summary_row = container.database.fetch_one(
        "SELECT summary_text_blob_id FROM summaries WHERE run_id = ?",
        (result.run_id,),
    )
    blob_dir = container.base_settings.resolved_protected_blob_dir
    referenced_blob_ids = container.data_governance_service.collect_blob_ids(json.loads(state_row["state_json"]))
    for row in job_rows:
        referenced_blob_ids.update(
            container.data_governance_service.collect_blob_ids(json.loads(row["result_json"] or "{}"))
        )
    assert referenced_blob_ids
    assert summary_row is not None
    assert summary_row["summary_text_blob_id"]
    assert (blob_dir / f"{summary_row['summary_text_blob_id']}.bin").exists()

    planner_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.node_type not in {"proposal", "summary"}
    )
    container.graph_runtime.request_retry(planner_node.id, actor="pytest", reason="purge orphan graph blobs")

    for blob_id in referenced_blob_ids:
        assert not (blob_dir / f"{blob_id}.bin").exists()
    assert (blob_dir / f"{summary_row['summary_text_blob_id']}.bin").exists()


def test_fail_closed_storage_refusal_surfaces_and_records_failed_graph(tmp_path):
    from app.config.settings import AppSettings

    settings = AppSettings(
        app_name="Fail Closed Graph Runtime Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        graph_execution_mode="inline_compat",
        local_protection_mode="unprotected-local",
        allow_insecure_local_storage=False,
        session_secret="test-session-secret",
    )
    container = AppContainer(settings)

    with pytest.raises(FailClosedStorageRefusalError, match="Strong local protection is required"):
        container.runtime_service.run_agent(
            AgentRunRequest(
                objective="Write protected file content",
                filesystem_path="secret.txt",
                file_content="restricted material",
            )
        )

    graph_row = container.database.fetch_one(
        "SELECT status, last_error, state_json FROM graph_runs ORDER BY created_at DESC LIMIT 1"
    )
    audit_row = container.database.fetch_one(
        "SELECT event_type, payload_json FROM audit_entries WHERE event_type = 'graph.node_failed' ORDER BY created_at DESC LIMIT 1"
    )

    assert graph_row is not None
    assert graph_row["status"] == "failed"
    assert "Strong local protection is required" in (graph_row["last_error"] or "")
    assert "restricted material" not in graph_row["state_json"]
    assert audit_row is not None
    assert "Strong local protection is required" in audit_row["payload_json"]


def test_graph_node_lease_is_extended_for_long_running_non_executor_job(app_settings, monkeypatch):
    container = AppContainer(app_settings.model_copy(update={"worker_lease_seconds": 1}))
    original_advance = container.graph_runtime.advance_run
    monkeypatch.setattr(
        container.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )
    original_execute = container.planner.execute_planner_node
    started = threading.Event()

    def slow_execute(*args, **kwargs):
        started.set()
        time.sleep(2.5)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(container.planner, "execute_planner_node", slow_execute)

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Long graph lease")
    )
    container.worker.run_once()
    container.worker.run_once()
    runner = threading.Thread(
        target=lambda: container.graph_runtime.run_next_non_executor_job(worker_id="graph-worker-a", run_id=result.run_id),
        daemon=True,
    )
    runner.start()
    assert started.wait(timeout=5)

    time.sleep(1.4)
    planner_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.node_type not in {"proposal", "summary"}
    )
    running_job = container.graph_node_queue_service.get_by_task_node_id(planner_node.id)
    competing_claim = container.graph_node_queue_service.claim_next_job(
        worker_id="graph-worker-b",
        lease_seconds=1,
        max_attempts=3,
        run_id=result.run_id,
    )

    runner.join(timeout=10)
    finished_job = container.graph_node_queue_service.get_by_task_node_id(planner_node.id)

    assert running_job.status == "running"
    assert running_job.last_heartbeat_at is not None
    assert competing_claim is None
    assert finished_job.status == "completed"
    assert finished_job.attempt_count == 1


def test_running_non_executor_job_supports_cooperative_cancel(app_settings, monkeypatch):
    container = AppContainer(app_settings.model_copy(update={"worker_lease_seconds": 1}))
    original_advance = container.graph_runtime.advance_run
    monkeypatch.setattr(
        container.graph_runtime,
        "advance_run",
        lambda run_id, max_steps=100, execute_inline=True: original_advance(
            run_id, max_steps=max_steps, execute_inline=False
        ),
    )
    original_build = container.planner._build_branch_proposals
    entered = threading.Event()

    def slow_build(*args, **kwargs):
        entered.set()
        time.sleep(1.5)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(container.planner, "_build_branch_proposals", slow_build)

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Inspect a local note", filesystem_path="notes.txt")
    )
    container.worker.run_once()
    container.worker.run_once()
    planner_node = next(
        node
        for node in container.agent_service.list_task_nodes(result.run_id)
        if node.role == AgentRole.PLANNER and node.branch_key == "filesystem"
    )
    runner = threading.Thread(
        target=lambda: container.graph_runtime.run_next_non_executor_job(worker_id="graph-worker-a", run_id=result.run_id),
        daemon=True,
    )
    runner.start()
    assert entered.wait(timeout=5)

    container.graph_runtime.cancel_node(planner_node.id, actor="pytest", reason="cancel running planner")
    runner.join(timeout=10)

    cancelled_nodes = container.agent_service.list_task_nodes(result.run_id)
    cancelled_planner = next(node for node in cancelled_nodes if node.id == planner_node.id)
    blocked_review = next(
        node
        for node in cancelled_nodes
        if node.role == AgentRole.REVIEWER and node.node_type == "review"
    )
    cancelled_job = container.graph_node_queue_service.get_by_task_node_id(planner_node.id)

    assert cancelled_planner.status == "cancelled"
    assert blocked_review.status == "blocked"
    assert cancelled_job is not None
    assert cancelled_job.status == "cancelled"
    assert cancelled_job.cancel_requested_at is not None

    container.graph_runtime.request_retry(planner_node.id, actor="pytest", reason="retry cancelled planner")
    container.graph_runtime.advance_run(result.run_id)
    for _ in range(12):
        if container.worker.run_once() is None:
            break

    retried_nodes = container.agent_service.list_task_nodes(result.run_id)
    retried_planner = next(node for node in retried_nodes if node.id == planner_node.id)
    assert retried_planner.status == "completed"


def test_worker_fairness_alternates_between_graph_and_executor_work(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Prepare executor work",
            task_title="Executor fairness task",
        )
    )
    proposal = result.proposals[0]
    queued = container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="executor fairness"),
    )

    container.base_settings.graph_execution_mode = "background_preferred"
    graph_result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Queue graph work for fairness",
            task_title="Graph fairness task",
        )
    )
    assert graph_result.summary is None

    first = container.worker.run_once()
    second = container.worker.run_once()

    assert first["status"] == "completed"
    assert second["status"] == "open"
    assert container.execution_queue_service.get(queued["job"].id).status.value == "executed"
