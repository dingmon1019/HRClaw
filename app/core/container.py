from __future__ import annotations

from app.agents.service import AgentService
from app.audit.service import AuditService
from app.config.settings import AppSettings, get_app_settings
from app.connectors.registry import ConnectorRegistry
from app.core.database import Database
from app.memory.service import SummaryService
from app.policy.engine import PolicyEngine
from app.providers.registry import ProviderRegistry
from app.runtime.execution_boundary import ConstrainedExecutionRunner
from app.runtime.executor import ExecutionDispatcher
from app.runtime.graph_runtime import GraphRuntimeService
from app.runtime.planner import RuntimePlanner
from app.runtime.service import AgentRuntimeService
from app.runtime.worker import ExecutionWorker
from app.security.admin_token import AdminTokenService
from app.security.protected_storage import ProtectedStorageService
from app.security.rate_limit import RateLimiter
from app.security.windows_credential_store import WindowsCredentialStore
from app.services.auth_service import AuthService
from app.services.artifact_lineage_service import ArtifactLineageService
from app.services.agent_workspace_service import AgentWorkspaceService
from app.services.cli_token_service import CliTokenService
from app.services.data_governance_service import DataGovernanceService
from app.services.execution_queue_service import ExecutionQueueService
from app.services.graph_node_queue_service import GraphNodeQueueService
from app.services.history_service import HistoryService
from app.services.proposal_service import ProposalService
from app.services.proposal_snapshot_service import ProposalSnapshotService
from app.services.provider_service import ProviderService
from app.services.session_service import SessionService
from app.services.settings_service import SettingsService


class AppContainer:
    def __init__(self, base_settings: AppSettings | None = None):
        self.base_settings = base_settings or get_app_settings()
        self.base_settings.resolved_runtime_state_root.mkdir(parents=True, exist_ok=True)
        self.base_settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        self.base_settings.resolved_secrets_dir.mkdir(parents=True, exist_ok=True)
        self.base_settings.resolved_protected_blob_dir.mkdir(parents=True, exist_ok=True)
        self.base_settings.resolved_logs_dir.mkdir(parents=True, exist_ok=True)
        self.base_settings.resolved_workspace_root.mkdir(parents=True, exist_ok=True)
        self.database = Database(self.base_settings.resolved_database_path)
        self.database.initialize()

        self.settings_service = SettingsService(self.base_settings, self.database)
        self.auth_service = AuthService(self.database)
        self.protected_storage = ProtectedStorageService(self.base_settings)
        self.windows_credential_store = WindowsCredentialStore()
        self.session_service = SessionService(self.database, self.base_settings)
        self.admin_token_service = AdminTokenService(self.database, self.auth_service, self.base_settings)
        self.data_governance_service = DataGovernanceService(self.protected_storage)
        self.agent_workspace_service = AgentWorkspaceService(self.base_settings)
        self.artifact_lineage_service = ArtifactLineageService(self.database, self.base_settings)
        self.cli_token_service = CliTokenService(self.database, self.auth_service, self.base_settings)
        self.rate_limiter = RateLimiter()
        self.history_service = HistoryService(self.database)
        self.agent_service = AgentService(self.database)
        self.audit_service = AuditService(
            self.database,
            self.base_settings.resolved_audit_log_path,
            self.settings_service,
            self.data_governance_service,
        )
        self.proposal_snapshot_service = ProposalSnapshotService(
            self.base_settings,
            self.database,
            self.settings_service,
            self.data_governance_service,
        )
        self.proposal_service = ProposalService(
            self.database,
            self.proposal_snapshot_service,
            self.data_governance_service,
        )
        self.execution_queue_service = ExecutionQueueService(self.database)
        self.graph_node_queue_service = GraphNodeQueueService(self.database)
        self.summary_service = SummaryService(self.database, self.protected_storage)
        self.policy_engine = PolicyEngine(self.base_settings, self.settings_service)
        self.graph_runtime = GraphRuntimeService(
            self.database,
            self.proposal_service,
            self.execution_queue_service,
            self.graph_node_queue_service,
            self.agent_service,
        )

        self.provider_registry = ProviderRegistry(self.base_settings)
        self.provider_service = ProviderService(
            self.provider_registry,
            self.settings_service,
            self.database,
            self.audit_service,
            self.windows_credential_store,
        )
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
            agent_service=self.agent_service,
            data_governance_service=self.data_governance_service,
            agent_workspace_service=self.agent_workspace_service,
            artifact_lineage_service=self.artifact_lineage_service,
        )
        self.planner.attach_graph_runtime(self.graph_runtime)
        self.graph_runtime.attach_planner(self.planner)
        self.execution_boundary_runner = ConstrainedExecutionRunner(self.base_settings)
        self.executor = ExecutionDispatcher(
            connector_registry=self.connector_registry,
            proposal_service=self.proposal_service,
            history_service=self.history_service,
            policy_engine=self.policy_engine,
            audit_service=self.audit_service,
            snapshot_service=self.proposal_snapshot_service,
            agent_service=self.agent_service,
            data_governance_service=self.data_governance_service,
            boundary_runner=self.execution_boundary_runner,
            artifact_lineage_service=self.artifact_lineage_service,
        )
        self.worker = ExecutionWorker(
            worker_id="local-worker",
            base_settings=self.base_settings,
            queue_service=self.execution_queue_service,
            graph_node_queue_service=self.graph_node_queue_service,
            proposal_service=self.proposal_service,
            dispatcher=self.executor,
            audit_service=self.audit_service,
            agent_service=self.agent_service,
            data_governance_service=self.data_governance_service,
            graph_runtime=self.graph_runtime,
        )
        self.runtime_service = AgentRuntimeService(
            planner=self.planner,
            queue_service=self.execution_queue_service,
            proposal_service=self.proposal_service,
            audit_service=self.audit_service,
            agent_service=self.agent_service,
            graph_runtime=self.graph_runtime,
        )
        self.graph_runtime.reconcile_all()
        self.graph_runtime.resume_all()
