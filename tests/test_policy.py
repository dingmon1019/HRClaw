from __future__ import annotations

from pathlib import Path

from app.schemas.actions import ActionProposal, ProposalStatus


def test_policy_allows_workspace_filesystem_read(container):
    target = container.base_settings.resolved_workspace_root / "allowed.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    proposal = ActionProposal(
        run_id="run_test",
        objective="Read the file",
        connector="filesystem",
        action_type="filesystem.read_text",
        title="Read file",
        description="Read file",
        payload={"path": str(target)},
    )

    evaluated = container.policy_engine.evaluate(proposal)

    assert evaluated.status == ProposalStatus.PENDING
    assert evaluated.side_effecting is False
    assert evaluated.requires_approval is True


def test_policy_blocks_path_outside_allowlist(container, tmp_path: Path):
    outside_path = tmp_path / "outside.txt"
    proposal = ActionProposal(
        run_id="run_test",
        objective="Read outside file",
        connector="filesystem",
        action_type="filesystem.read_text",
        title="Read outside file",
        description="Read outside file",
        payload={"path": str(outside_path)},
    )

    evaluated = container.policy_engine.evaluate(proposal)

    assert evaluated.status == ProposalStatus.BLOCKED
    assert any("outside the configured workspace allowlist" in note for note in evaluated.policy_notes)


def test_policy_blocks_writes_to_protected_source_paths(container):
    proposal = ActionProposal(
        run_id="run_test",
        objective="Overwrite the README",
        connector="filesystem",
        action_type="filesystem.write_text",
        title="Overwrite README",
        description="Write source-controlled file",
        payload={"path": str(container.base_settings.project_root / "README.md"), "content": "nope"},
    )

    evaluated = container.policy_engine.evaluate(proposal)

    assert evaluated.status == ProposalStatus.BLOCKED
    assert any(
        "protected path" in note or "outside the configured workspace allowlist" in note
        for note in evaluated.policy_notes
    )


def test_policy_blocks_private_network_http_by_default(container):
    proposal = ActionProposal(
        run_id="run_test",
        objective="Call localhost",
        connector="http",
        action_type="http.get",
        title="GET localhost",
        description="Loopback request",
        payload={"url": "http://127.0.0.1:8000/health"},
    )

    evaluated = container.policy_engine.evaluate(proposal)

    assert evaluated.status == ProposalStatus.BLOCKED
    assert any("blocked by policy" in note for note in evaluated.policy_notes)


def test_policy_blocks_unknown_system_action(container):
    proposal = ActionProposal(
        run_id="run_test",
        objective="Run something unsafe",
        connector="system",
        action_type="system.exec_anything",
        title="Unsafe system action",
        description="Not allowed",
        payload={"path": "notes.txt"},
    )

    evaluated = container.policy_engine.evaluate(proposal)

    assert evaluated.status == ProposalStatus.BLOCKED
    assert any("not allowed" in note for note in evaluated.policy_notes)
