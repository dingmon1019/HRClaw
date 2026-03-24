from __future__ import annotations

import json
from pathlib import Path
import subprocess

from app.core.container import AppContainer
from app.schemas.actions import ExecutionBoundaryMetadata
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
from app.schemas.agents import AgentRole


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

    assert command[0].endswith("python.exe") or command[0].endswith("python")
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
