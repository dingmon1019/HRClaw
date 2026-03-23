from __future__ import annotations

import os

import pytest

from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
from app.schemas.providers import ProviderRequest
from app.schemas.settings import SettingsUpdate
from tests.helpers import bootstrap_operator


def _settings_update_from_effective(settings) -> SettingsUpdate:
    return SettingsUpdate(
        runtime_mode=settings.runtime_mode,
        provider=settings.provider,
        fallback_provider=settings.fallback_provider,
        model=settings.model,
        base_url=settings.base_url,
        api_key_env=settings.api_key_env,
        generic_http_endpoint=settings.generic_http_endpoint,
        provider_timeout_seconds=settings.provider_timeout_seconds,
        provider_max_retries=settings.provider_max_retries,
        provider_circuit_breaker_threshold=settings.provider_circuit_breaker_threshold,
        provider_circuit_breaker_seconds=settings.provider_circuit_breaker_seconds,
        summary_profile=settings.summary_profile,
        planning_profile=settings.planning_profile,
        fast_provider=settings.fast_provider,
        cheap_provider=settings.cheap_provider,
        strong_provider=settings.strong_provider,
        local_provider=settings.local_provider,
        privacy_provider=settings.privacy_provider,
        provider_allowed_hosts=",".join(settings.provider_allowed_hosts),
        allow_provider_private_network=settings.allow_provider_private_network,
        allow_restricted_provider_egress=settings.allow_restricted_provider_egress,
        json_audit_enabled=settings.json_audit_enabled,
        session_max_age_seconds=settings.session_max_age_seconds,
        session_idle_timeout_seconds=settings.session_idle_timeout_seconds,
        recent_auth_window_seconds=settings.recent_auth_window_seconds,
        max_request_size_bytes=settings.max_request_size_bytes,
        allowed_http_schemes=",".join(settings.allowed_http_schemes),
        allowed_http_ports=",".join(str(port) for port in settings.allowed_http_ports),
        allow_http_private_network=settings.allow_http_private_network,
        http_follow_redirects=settings.http_follow_redirects,
        http_timeout_seconds=settings.http_timeout_seconds,
        http_max_response_bytes=settings.http_max_response_bytes,
        filesystem_max_read_bytes=settings.filesystem_max_read_bytes,
        allowed_filesystem_roots=",".join(settings.allowed_filesystem_roots),
        allowed_http_hosts=",".join(settings.allowed_http_hosts),
        enable_system_connector=settings.enable_system_connector,
        enable_outlook_connector=settings.enable_outlook_connector,
        worker_lease_seconds=settings.worker_lease_seconds,
        worker_max_attempts=settings.worker_max_attempts,
    )


def test_session_idle_timeout_forces_relogin(client, container):
    bootstrap_operator(client)
    original_now = container.auth_service.now_epoch()
    container.auth_service.now_epoch = lambda: original_now + container.settings_service.get_effective_settings().session_idle_timeout_seconds + 1

    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_approved_snapshot_becomes_stale_when_settings_change(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Snapshot task")
    )
    proposal = result.proposals[0]
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="Approve initial snapshot"),
    )

    current = container.settings_service.get_effective_settings()
    update = _settings_update_from_effective(current)
    update.max_request_size_bytes = current.max_request_size_bytes + 128
    container.settings_service.save(update, actor="pytest", reason="force-stale")

    with pytest.raises(ValueError, match="Snapshot drift detected"):
        container.worker.run_once()

    updated = container.proposal_service.get(proposal.id)
    assert updated.status == ProposalStatus.STALE
    assert updated.stale_reason


def test_path_guard_blocks_symlink_escape(container, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    link_path = container.base_settings.resolved_workspace_root / "escape.txt"
    try:
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        os.symlink(outside, link_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not available in this Windows test environment.")

    message = container.policy_engine.path_guard.check_payload({"path": str(link_path)}, write=False)

    assert message is not None
    assert "Symlink traversal is not allowed" in message


def test_provider_restricted_data_stays_local(tmp_path):
    settings = AppSettings(
        app_name="Restricted Egress Test",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        provider_allowed_hosts="api.openai.com",
        allow_restricted_provider_egress=False,
        session_secret="test-session-secret",
    )
    container = AppContainer(settings)

    response = container.provider_service.complete(
        ProviderRequest(prompt="Keep restricted data local.", data_classification="restricted")
    )

    assert response.provider_name == "mock"


def test_worker_can_reclaim_stale_running_job(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Lease task")
    )
    proposal = result.proposals[0]
    queued = container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="queue for lease test"),
    )

    claimed = container.execution_queue_service.claim_next_job("worker-a", lease_seconds=1, max_attempts=3)
    assert claimed is not None
    job, attempt = claimed
    assert attempt.attempt_number == 1

    container.database.execute(
        "UPDATE execution_jobs SET lease_expires_at = ?, status = 'running' WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", job.id),
    )

    reclaimed = container.execution_queue_service.claim_next_job("worker-b", lease_seconds=30, max_attempts=3)
    assert reclaimed is not None
    reclaimed_job, reclaimed_attempt = reclaimed
    assert reclaimed_job.id == queued["job"].id
    assert reclaimed_job.worker_id == "worker-b"
    assert reclaimed_attempt.attempt_number == 2


def test_json_audit_disable_keeps_database_audit(container):
    settings = container.settings_service.get_effective_settings()
    update = _settings_update_from_effective(settings)
    update.json_audit_enabled = False
    container.settings_service.save(update, actor="pytest", reason="disable-json")

    container.audit_service.emit("test.db_only", {"value": 1})

    row = container.database.fetch_one("SELECT event_type FROM audit_entries WHERE event_type = ?", ("test.db_only",))
    assert row is not None
    assert row["event_type"] == "test.db_only"


def test_multi_agent_handoffs_are_persisted(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Review and propose a safe task",
            task_title="Agent persistence",
            task_details="Record every handoff",
        )
    )

    agent_runs = container.agent_service.list_run_history(result.run_id)
    handoffs = container.agent_service.list_handoffs(result.run_id)
    roles = {run.role.value for run in agent_runs}

    assert {"supervisor", "planner", "reviewer", "reporter"}.issubset(roles)
    assert len(handoffs) >= 3
    assert all(proposal.created_by_agent_role == "planner" for proposal in result.proposals)
    assert all(proposal.reviewed_by_agent_role == "reviewer" for proposal in result.proposals)
