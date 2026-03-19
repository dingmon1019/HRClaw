from __future__ import annotations

from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.schemas.actions import AgentRunRequest


def test_database_persists_between_container_instances(app_settings: AppSettings):
    container_a = AppContainer(app_settings)
    result = container_a.runtime_service.run_agent(
        AgentRunRequest(objective="Create a persisted task", task_title="Persisted task")
    )

    container_b = AppContainer(app_settings)
    proposals = container_b.proposal_service.list()
    summaries = container_b.summary_service.list_recent()

    assert any(proposal.id == result.proposals[0].id for proposal in proposals)
    assert summaries

