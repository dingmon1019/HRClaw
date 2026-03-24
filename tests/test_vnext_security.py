from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from app import cli as cli_module
from app.api.app import create_app
from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.core.errors import AuthorizationError
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
from app.schemas.providers import ProviderRequest, ProviderResponse
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
        local_protection_mode=settings.local_protection_mode,
        allow_insecure_local_storage=settings.allow_insecure_local_storage,
        history_retention_days=settings.history_retention_days,
        cli_token_ttl_seconds=settings.cli_token_ttl_seconds,
        worker_lease_seconds=settings.worker_lease_seconds,
        worker_max_attempts=settings.worker_max_attempts,
    )


def test_session_idle_timeout_forces_relogin(client, container):
    bootstrap_operator(client)
    original_now = container.session_service.now_utc()
    container.session_service.now_utc = lambda: original_now + timedelta(
        seconds=container.settings_service.get_effective_settings().session_idle_timeout_seconds + 1
    )

    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_run_agent_page_uses_truthful_boundary_language(client):
    bootstrap_operator(client)

    response = client.get("/run", headers={"accept": "text/html"})
    body = response.text

    assert response.status_code == 200
    assert "Constrained Worker Boundary" in body
    assert "Worker Isolated" not in body


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
        runtime_state_root=tmp_path / "runtime-state",
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


def test_audit_chain_uses_insertion_order_when_timestamps_match(container, monkeypatch):
    monkeypatch.setattr("app.audit.service.utcnow_iso", lambda: "2026-03-23T00:00:00+00:00")

    container.audit_service.emit("test.same_second_a", {"value": 1})
    container.audit_service.emit("test.same_second_b", {"value": 2})

    result = container.audit_service.verify_integrity()

    assert result["ok"] is True
    assert result["entry_count"] >= 2


def test_worker_blocks_when_approval_binding_is_tampered(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Approval binding")
    )
    proposal = result.proposals[0]
    queued = container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="queue for approval binding test"),
    )

    container.database.execute(
        "UPDATE approvals SET action_hash = ? WHERE id = ?",
        ("tampered", queued["approval"].id),
    )

    with pytest.raises(ValueError, match="queued approval"):
        container.worker.run_once()

    updated = container.proposal_service.get(proposal.id)
    assert updated.status == ProposalStatus.STALE
    assert queued["job"].approval_id == queued["approval"].id


def test_live_running_job_is_not_reclaimed_before_lease_expires(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Create a tracked task", task_title="Lease guard")
    )
    proposal = result.proposals[0]
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="queue for live lease test"),
    )

    claimed = container.execution_queue_service.claim_next_job("worker-a", lease_seconds=30, max_attempts=3)
    assert claimed is not None

    second_claim = container.execution_queue_service.claim_next_job("worker-b", lease_seconds=30, max_attempts=3)

    assert second_claim is None


def test_planner_connector_permissions_are_enforced(container):
    container.database.execute(
        "UPDATE agents SET allowed_connectors_json = ? WHERE role = ?",
        ('[]', "planner"),
    )

    with pytest.raises(AuthorizationError, match="Planner Agent is not allowed to use connector"):
        container.runtime_service.run_agent(
            AgentRunRequest(objective="Read a file", filesystem_path="notes.txt")
        )


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
    connector_runs = container.history_service.list_connector_runs(limit=20)
    assert all(run.agent_role for run in connector_runs)


def test_task_graph_nodes_are_persisted(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Review a file and create a task",
            filesystem_path="notes.txt",
            task_title="Trace graph",
        )
    )

    task_nodes = container.agent_service.list_task_nodes(result.run_id)

    assert any(node.node_type == "objective" for node in task_nodes)
    assert any(node.node_type == "proposal" for node in task_nodes)
    assert any(node.node_type == "merge" for node in task_nodes)
    assert any(node.role.value == "executor" for node in task_nodes)
    assert any(node.depends_on for node in task_nodes if node.node_type != "objective")


def test_default_runtime_state_uses_localappdata(monkeypatch, tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    settings = AppSettings(_env_file=None)

    assert settings.resolved_runtime_state_root == (local_appdata / "WinAgentRuntime").resolve()
    assert settings.resolved_database_path == (local_appdata / "WinAgentRuntime" / "data" / "win_agent_runtime.db").resolve()
    assert settings.resolved_session_secret_path == (local_appdata / "WinAgentRuntime" / "secrets" / "session_secret.bin").resolve()


def test_sensitive_payload_is_externalized_from_proposal_rows(container):
    secret_text = "super-sensitive proposal body"
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Write a note safely",
            filesystem_path="note.txt",
            file_content=secret_text,
        )
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "filesystem.write_text")

    row = container.database.fetch_one("SELECT payload_json FROM proposals WHERE id = ?", (proposal.id,))
    assert row is not None
    assert secret_text not in row["payload_json"]
    assert proposal.payload.get("content_blob_id")

    materialized = container.data_governance_service.materialize_action_payload(proposal.payload)
    assert materialized["content"] == secret_text


def test_sensitive_request_material_is_not_duplicated_in_agent_runs(container):
    secret_text = "duplicate-me-not"
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Write a note safely",
            filesystem_path="note.txt",
            file_content=secret_text,
        )
    )

    rows = container.database.fetch_all("SELECT input_json, output_json FROM agent_runs WHERE run_id = ?", (result.run_id,))

    assert rows
    combined = "\n".join(f"{row['input_json']}\n{row['output_json']}" for row in rows)
    assert secret_text not in combined


def test_fail_closed_storage_blocks_sensitive_payload_without_override(tmp_path):
    settings = AppSettings(
        app_name="Fail Closed Storage Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        local_protection_mode="unprotected-local",
        allow_insecure_local_storage=False,
        session_secret="test-session-secret",
    )
    container = AppContainer(settings)

    with pytest.raises(ValueError, match="Strong local protection is required"):
        container.runtime_service.run_agent(
            AgentRunRequest(
                objective="Write protected file content",
                filesystem_path="secret.txt",
                file_content="restricted material",
            )
        )


def test_generated_session_secret_requires_strong_protection_or_override(tmp_path):
    settings = AppSettings(
        app_name="Session Secret Protection Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        local_protection_mode="unprotected-local",
        allow_insecure_local_storage=False,
        session_secret=None,
    )

    with pytest.raises(ValueError, match="Strong local protection is required to store session-secret"):
        create_app(settings)


def test_secret_text_file_is_refused_when_only_unprotected_storage_is_available(tmp_path):
    insecure_settings = AppSettings(
        app_name="Secret File Protection Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        local_protection_mode="unprotected-local",
        allow_insecure_local_storage=True,
        session_secret="test-session-secret",
    )
    insecure_container = AppContainer(insecure_settings)
    secret_path = insecure_container.base_settings.resolved_secrets_dir / "legacy-token.bin"
    insecure_container.protected_storage.write_secret_text(secret_path, "temporary-token", purpose="cli-token")

    strict_settings = insecure_settings.model_copy(update={"allow_insecure_local_storage": False})
    strict_container = AppContainer(strict_settings)

    with pytest.raises(ValueError, match="cli-token is stored with unprotected local fallback"):
        strict_container.protected_storage.read_secret_text(secret_path, purpose="cli-token")


def test_history_sanitization_redacts_sensitive_fields(container):
    sanitized = container.data_governance_service.sanitize_for_history(
        {
            "content": "top-secret",
            "body": "remote-body",
            "details": "operator notes",
            "headers": {"Authorization": "Bearer secret-token", "Accept": "application/json"},
            "rationale": "this should not be stored in full",
        }
    )

    assert sanitized["content"]["redacted"] is True
    assert sanitized["body"]["redacted"] is True
    assert sanitized["details"]["redacted"] is True
    assert sanitized["headers"]["Authorization"]["redacted"] is True
    assert sanitized["headers"]["Accept"] == "application/json"
    assert "this should not be stored in full"[:32] in sanitized["rationale"]
    assert "top-secret" not in json.dumps(sanitized)


def test_object_aware_agent_input_redaction(container):
    sanitized = container.data_governance_service.sanitize_for_history(
        {
            "objective": "Review a file",
            "request": {
                "filesystem_path": "secret.txt",
                "file_content": "do not duplicate",
            },
        },
        object_type="agent_input",
    )

    assert sanitized["objective"].startswith("Review a file")
    assert sanitized["request"]["redacted"] is True
    assert "do not duplicate" not in json.dumps(sanitized)


def test_full_file_digest_detects_same_size_content_change(container):
    target = container.base_settings.resolved_workspace_root / "digest.txt"
    target.write_text("AAAA", encoding="utf-8")

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="Read digest file", filesystem_path="digest.txt")
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "filesystem.read_text")
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="approve file digest"),
    )

    target.write_text("BBBB", encoding="utf-8")

    with pytest.raises(ValueError, match="Snapshot drift detected"):
        container.worker.run_once()

    updated = container.proposal_service.get(proposal.id)
    assert updated.status == ProposalStatus.STALE


def test_directory_digest_detects_nested_same_size_content_change(container):
    folder = container.base_settings.resolved_workspace_root / "folder"
    folder.mkdir(exist_ok=True)
    nested = folder / "item.txt"
    nested.write_text("AAAA", encoding="utf-8")

    result = container.runtime_service.run_agent(
        AgentRunRequest(objective="List folder contents", filesystem_path="folder")
    )
    proposal = next(proposal for proposal in result.proposals if proposal.action_type == "filesystem.list_directory")
    container.runtime_service.approve_and_queue(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="approve folder digest"),
    )

    nested.write_text("BBBB", encoding="utf-8")

    with pytest.raises(ValueError, match="Snapshot drift detected"):
        container.worker.run_once()

    updated = container.proposal_service.get(proposal.id)
    assert updated.status == ProposalStatus.STALE


def test_remote_planning_prompt_redacts_local_task_snapshot(monkeypatch, tmp_path):
    settings = AppSettings(
        app_name="Remote Prompt Governance Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        session_secret="test-session-secret",
        allow_insecure_local_storage=True,
    )
    container = AppContainer(settings)
    container.database.execute(
        "INSERT INTO tasks(id, title, details, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("task_secret", "Secret task", "TOP SECRET TASK DETAILS", "open", "2026-03-24T00:00:00+00:00", "2026-03-24T00:00:00+00:00"),
    )
    captured: list[ProviderRequest] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        container.provider_registry.get("openai"),
        "complete",
        lambda request, provider_settings: (
            captured.append(request)
            or ProviderResponse(
                provider_name="openai",
                model_name=request.model_name or provider_settings.model,
                content="remote summary",
                raw_response={},
            )
        ),
    )

    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Plan a follow-up from the current local task backlog",
            task_title="Follow up locally",
            provider_name="openai",
        )
    )

    assert result.summary.provider_name == "openai"
    assert captured
    planning_request = next(request for request in captured if request.task_type == "planning-summary")
    assert "TOP SECRET TASK DETAILS" not in planning_request.prompt
    assert '"details"' not in planning_request.prompt
    assert "task_snapshot_present" in planning_request.prompt
    assert planning_request.metadata["selected_prompt_variant"] == "remote"


def test_remote_planning_prompt_redacts_filesystem_context(monkeypatch, tmp_path):
    settings = AppSettings(
        app_name="Remote Filesystem Prompt Governance Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        session_secret="test-session-secret",
        allow_insecure_local_storage=True,
    )
    workspace_file = Path(settings.workspace_root) / "notes.txt"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("TOP SECRET FILE CONTENT", encoding="utf-8")
    container = AppContainer(settings)
    captured: list[ProviderRequest] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        container.connector_registry.get("filesystem"),
        "collect",
        lambda payload: (_ for _ in ()).throw(AssertionError("planning should not read filesystem content before approval")),
    )
    monkeypatch.setattr(
        container.provider_registry.get("openai"),
        "complete",
        lambda request, provider_settings: (
            captured.append(request)
            or ProviderResponse(
                provider_name="openai",
                model_name=request.model_name or provider_settings.model,
                content="remote summary",
                raw_response={},
            )
        ),
    )

    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Inspect a local note and propose the next safe step",
            filesystem_path="notes.txt",
            provider_name="openai",
        )
    )

    planning_request = next(request for request in captured if request.task_type == "planning-summary")
    assert "TOP SECRET FILE CONTENT" not in planning_request.prompt
    assert str(workspace_file) not in planning_request.prompt
    assert "path_redacted" in planning_request.prompt
    assert "inspection_deferred" in planning_request.prompt
    assert any(proposal.action_type == "filesystem.read_text" for proposal in result.proposals)
    assert result.summary.collected["filesystem"]["collection_mode"] == "descriptor-only"
    assert result.summary.collected["deferred_evidence"][0]["action_type"] == "filesystem.read_text"


def test_remote_report_prompt_withholds_local_only_derived_summary(monkeypatch, tmp_path):
    settings = AppSettings(
        app_name="Remote Report Governance Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        session_secret="test-session-secret",
        allow_insecure_local_storage=True,
    )
    container = AppContainer(settings)
    container.database.execute(
        "INSERT INTO tasks(id, title, details, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("task_report_secret", "Secret task", "ULTRA SECRET TASK PAYLOAD", "open", "2026-03-24T00:00:00+00:00", "2026-03-24T00:00:00+00:00"),
    )
    captured: list[ProviderRequest] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def complete(request, provider_settings):
        captured.append(request)
        if request.task_type == "planning-summary":
            return ProviderResponse(
                provider_name="openai",
                model_name=request.model_name or provider_settings.model,
                content="TOP SECRET DERIVED SUMMARY FROM LOCAL TASK SNAPSHOT",
                raw_response={},
            )
        return ProviderResponse(
            provider_name="openai",
            model_name=request.model_name or provider_settings.model,
            content="remote report",
            raw_response={},
        )

    monkeypatch.setattr(container.provider_registry.get("openai"), "complete", complete)

    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Plan a follow-up from local task data and summarize it for me",
            task_title="Follow up safely",
            provider_name="openai",
        )
    )

    summary = container.summary_service.get_by_run_id(result.run_id)
    assert summary is not None
    assert summary.data_classification.value == "local-only"
    assert summary.outbound_summary_text is None

    report_request = next(request for request in captured if request.task_type == "report-plan")
    assert "TOP SECRET DERIVED SUMMARY FROM LOCAL TASK SNAPSHOT" not in report_request.prompt
    assert "summary-withheld" in report_request.prompt
    assert report_request.metadata["selected_prompt_variant"] == "remote"
    assert report_request.metadata["prompt_governance"]["lineage"]["source_classification"] == "local-only"

    outbound_rows = container.database.fetch_all(
        "SELECT payload_json FROM audit_entries WHERE event_type = ? ORDER BY rowid ASC",
        ("provider.prompt_outbound",),
    )
    report_outbound = json.loads(outbound_rows[-1]["payload_json"])
    assert report_outbound["task_type"] == "report-plan"
    assert report_outbound["prompt_governance"]["lineage"]["source_classification"] == "local-only"
    assert report_outbound["prompt_governance"]["curation_posture"] == "summary-withheld"


def test_remote_planning_prompt_redacts_http_response_body_preview(monkeypatch, tmp_path):
    settings = AppSettings(
        app_name="Remote HTTP Prompt Governance Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        allowed_http_hosts="example.com",
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        session_secret="test-session-secret",
        allow_insecure_local_storage=True,
    )
    container = AppContainer(settings)
    captured: list[ProviderRequest] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        container.connector_registry.get("http"),
        "collect",
        lambda payload: (_ for _ in ()).throw(AssertionError("planning should not fetch HTTP content before approval")),
    )
    monkeypatch.setattr(
        container.provider_registry.get("openai"),
        "complete",
        lambda request, provider_settings: (
            captured.append(request)
            or ProviderResponse(
                provider_name="openai",
                model_name=request.model_name or provider_settings.model,
                content="remote summary",
                raw_response={},
            )
        ),
    )

    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Inspect an allowlisted HTTP endpoint and propose the next safe step",
            http_url="https://example.com/api",
            provider_name="openai",
        )
    )

    planning_request = next(request for request in captured if request.task_type == "planning-summary")
    assert "REMOTE RESPONSE BODY" not in planning_request.prompt
    assert "body_preview" not in planning_request.prompt
    assert "fetch_deferred" in planning_request.prompt
    assert planning_request.metadata["selected_prompt_variant"] == "remote"
    assert planning_request.metadata["prompt_governance"]["lineage"]["source_classification"] == "external-ok"
    assert result.summary.data_classification.value == "external-ok"
    assert any(proposal.action_type == "http.get" for proposal in result.proposals)
    assert result.summary.collected["http"]["collection_mode"] == "descriptor-only"


def test_cli_token_issue_and_verify(container):
    username = "cli-operator"
    password = "CliSecure123!"
    container.auth_service.create_initial_user(username, password)

    token, record = container.cli_token_service.issue(
        username=username,
        password=password,
        purpose="worker",
        ttl_seconds=60,
    )
    verified = container.cli_token_service.verify(token, purpose="worker")

    assert verified.id == record.id
    assert verified.username == username

    with pytest.raises(AuthorizationError):
        container.cli_token_service.verify(token, purpose="approval")


def test_cli_sensitive_command_requires_short_lived_token(container, monkeypatch):
    monkeypatch.setattr(cli_module, "AppContainer", lambda: container)
    monkeypatch.setattr(sys, "argv", ["app.cli", "run-worker", "--once"])

    with pytest.raises(AuthorizationError, match="Sensitive CLI commands require either --token-file or --username"):
        cli_module.main()


def test_cli_issue_parser_rejects_password_argument():
    parser = cli_module.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["issue-cli-token", "--username", "operator", "--password", "bad"])


def test_cli_sensitive_command_uses_secure_prompt_without_token_env(container, monkeypatch, capsys):
    username = "prompt-operator"
    password = "CliSecure123!"
    container.auth_service.create_initial_user(username, password)
    monkeypatch.setattr(cli_module, "AppContainer", lambda: container)
    monkeypatch.setattr(sys, "argv", ["app.cli", "run-worker", "--once", "--username", username])
    monkeypatch.setattr(cli_module, "getpass", lambda _: password)

    cli_module.main()

    output = capsys.readouterr().out
    assert "No queued jobs." in output
    assert "CliSecure123!" not in output


def test_cli_token_file_mode_is_disabled_without_strong_protection(container, monkeypatch):
    username = "token-file-operator"
    password = "CliSecure123!"
    container.auth_service.create_initial_user(username, password)
    strict_settings = container.base_settings.model_copy(
        update={
            "local_protection_mode": "unprotected-local",
            "allow_insecure_local_storage": False,
        }
    )
    strict_container = AppContainer(strict_settings)
    monkeypatch.setattr(cli_module, "AppContainer", lambda: strict_container)
    monkeypatch.setattr(sys, "argv", ["app.cli", "issue-cli-token", "--username", username, "--purpose", "worker", "--token-file", "worker.token"])
    monkeypatch.setattr(cli_module, "getpass", lambda _: password)

    with pytest.raises(ValueError, match="Strong local protection is required to store cli-token"):
        cli_module.main()


def test_cli_parser_rejects_password_stdin_legacy_flag():
    parser = cli_module.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run-worker", "--once", "--username", "operator", "--password-stdin"])


def test_release_packaging_verifier_rejects_forbidden_paths(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"
    spec = importlib.util.spec_from_file_location("package_release", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    with pytest.raises(ValueError, match="Forbidden artifact path"):
        module._validate_relative(Path(".venv") / "Scripts" / "python.exe")


def test_release_archive_uses_allowlist(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"
    spec = importlib.util.spec_from_file_location("package_release", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "ui").mkdir()
    (root / "docs").mkdir()
    (root / "README.md").write_text("readme", encoding="utf-8")
    (root / "LICENSE").write_text("license", encoding="utf-8")
    (root / "main.py").write_text("print('ok')", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi", encoding="utf-8")
    (root / ".env.example").write_text("APP_NAME=Test", encoding="utf-8")
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts" / "bootstrap.ps1").write_text("Write-Host ok", encoding="utf-8")
    (root / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "docs" / "release_hygiene.md").write_text("docs", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "bad.txt").write_text("bad", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "runtime.db").write_text("bad", encoding="utf-8")

    module.DIST_DIR = tmp_path / "dist"
    result = module.build_archive(root, version="pytest")
    import zipfile

    with zipfile.ZipFile(result.archive_path, "r") as handle:
        names = set(handle.namelist())
        manifest_name = next(name for name in names if name.endswith("release_manifest.json"))
        manifest = json.loads(handle.read(manifest_name).decode("utf-8"))

    assert all(".venv/" not in name for name in names)
    assert all("/data/" not in name for name in names)
    assert any(name.endswith("README.md") for name in names)
    assert result.sha256_path is not None and result.sha256_path.exists()
    assert manifest["runtime_state_outside_repo"] is True
    assert manifest["runtime_state_outside_repo_statement"]
    assert manifest["excluded_policy"]["forbidden_segments"]
    assert manifest["include_policy"]
    assert manifest["included_paths"]


def test_handoff_preflight_flags_dist_artifacts(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"
    spec = importlib.util.spec_from_file_location("package_release", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    (tmp_path / "dist").mkdir()
    (tmp_path / "workspace").mkdir()

    findings = module.verify_working_tree(tmp_path, include_dist=True)

    assert "dist" in findings
    assert "workspace" in findings


def test_handoff_archive_excludes_runtime_and_dist_artifacts(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"
    spec = importlib.util.spec_from_file_location("package_release", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "ui").mkdir()
    (root / "docs").mkdir()
    (root / "README.md").write_text("readme", encoding="utf-8")
    (root / "LICENSE").write_text("license", encoding="utf-8")
    (root / "main.py").write_text("print('ok')", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi", encoding="utf-8")
    (root / ".env.example").write_text("APP_NAME=Test", encoding="utf-8")
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts" / "bootstrap.ps1").write_text("Write-Host ok", encoding="utf-8")
    (root / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "docs" / "release_hygiene.md").write_text("docs", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "stale.zip").write_text("bad", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "runtime.db").write_text("bad", encoding="utf-8")

    module.DIST_DIR = tmp_path / "dist-out"
    result = module.build_handoff_archive(root, version="pytest")
    import zipfile

    with zipfile.ZipFile(result.archive_path, "r") as handle:
        names = set(handle.namelist())
        manifest_name = next(name for name in names if name.endswith("release_manifest.json"))
        manifest = json.loads(handle.read(manifest_name).decode("utf-8"))

    assert all("/dist/" not in name for name in names)
    assert all("/data/" not in name for name in names)
    assert manifest["artifact_kind"] == "handoff-source"
    assert manifest["runtime_state_outside_repo"] is True
