from __future__ import annotations

from app.audit.service import AuditService
from app.config.settings import AppSettings, get_app_settings
from app.connectors.registry import ConnectorRegistry
from app.core.database import Database
from app.memory.service import SummaryService
from app.policy.engine import PolicyEngine
from app.providers.registry import ProviderRegistry
from app.runtime.executor import ExecutionDispatcher
from app.runtime.planner import RuntimePlanner
from app.runtime.service import AgentRuntimeService
from app.runtime.worker import ExecutionWorker
from app.security.rate_limit import RateLimiter
from app.services.auth_service import AuthService
from app.services.execution_queue_service import ExecutionQueueService
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService
from app.services.provider_service import ProviderService
from app.services.settings_service import SettingsService


class AppContainer:
    def __init__(self, base_settings: AppSettings | None = None):
        self.base_settings = base_settings or get_app_settings()
        self.base_settings.resolved_workspace_root.mkdir(parents=True, exist_ok=True)
        self.database = Database(self.base_settings.resolved_database_path)
        self.database.initialize()

        self.settings_service = SettingsService(self.base_settings, self.database)
        self.auth_service = AuthService(self.database)
        self.rate_limiter = RateLimiter()
        self.audit_service = AuditService(self.database, self.base_settings.resolved_audit_log_path, self.settings_service)
        self.history_service = HistoryService(self.database)
        self.proposal_service = ProposalService(self.database)
        self.execution_queue_service = ExecutionQueueService(self.database)
        self.summary_service = SummaryService(self.database)
        self.policy_engine = PolicyEngine(self.base_settings, self.settings_service)

        self.provider_registry = ProviderRegistry(self.base_settings)
        self.provider_service = ProviderService(self.provider_registry, self.settings_service)
        self.connector_registry = ConnectorRegistry(self.base_settings, self.database, self.settings_service)

        self.planner = RuntimePlanner(
            base_settings=self.base_settings,
            connector_registry=self.connector_registry,
            provider_service=self.provider_service,
            summary_service=self.summary_service,
            proposal_service=self.proposal_service,
            history_service=self.history_service,
            policy_engine=self.policy_engine,
            audit_service=self.audit_service,
        )
        self.executor = ExecutionDispatcher(
            connector_registry=self.connector_registry,
            proposal_service=self.proposal_service,
            history_service=self.history_service,
            policy_engine=self.policy_engine,
            audit_service=self.audit_service,
        )
        self.worker = ExecutionWorker(
            worker_id="local-worker",
            queue_service=self.execution_queue_service,
            proposal_service=self.proposal_service,
            dispatcher=self.executor,
            audit_service=self.audit_service,
        )
        self.runtime_service = AgentRuntimeService(
            planner=self.planner,
            queue_service=self.execution_queue_service,
            proposal_service=self.proposal_service,
            audit_service=self.audit_service,
        )
