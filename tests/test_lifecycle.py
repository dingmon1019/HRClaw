from __future__ import annotations

from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus


def test_proposal_lifecycle_from_run_to_execution(container):
    result = container.runtime_service.run_agent(
        AgentRunRequest(
            objective="Create a tracked task for the operator",
            task_title="Follow up on operator task",
            task_details="Check pending work",
        )
    )

    proposal = result.proposals[0]
    assert proposal.status == ProposalStatus.PENDING

    execution = container.runtime_service.approve_and_execute(
        proposal.id,
        ApprovalDecisionRequest(actor="pytest", reason="Lifecycle test"),
    )

    updated = container.proposal_service.get(proposal.id)
    assert updated.status == ProposalStatus.EXECUTED
    assert execution["result"]["status"] == "open"
    assert container.proposal_service.list_approvals(proposal.id)[0].decision == "approved"


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

