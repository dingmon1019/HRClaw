from __future__ import annotations

from app.schemas.actions import ExecutionBoundaryMetadata
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus


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
