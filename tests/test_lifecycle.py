from __future__ import annotations

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
    assert updated.status == ProposalStatus.EXECUTED
    assert worker_result["status"] == "open"
    assert container.execution_queue_service.get(queued["job"].id).status.value == "executed"
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
