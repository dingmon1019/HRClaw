from __future__ import annotations

from pathlib import Path

from app.schemas.actions import ActionProposal, ProposalStatus


def test_policy_allows_allowlisted_filesystem_read(container, tmp_path: Path):
    target = tmp_path / "allowed.txt"
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
    outside_path = tmp_path.parent / "outside.txt"
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
    assert any("outside the filesystem allowlist" in note for note in evaluated.policy_notes)


def test_policy_blocks_non_allowlisted_powershell(container):
    proposal = ActionProposal(
        run_id="run_test",
        objective="Delete something",
        connector="system",
        action_type="system.powershell",
        title="Run PowerShell",
        description="Run disallowed command",
        payload={"command": "Remove-Item .\\foo.txt"},
    )

    evaluated = container.policy_engine.evaluate(proposal)

    assert evaluated.status == ProposalStatus.BLOCKED

