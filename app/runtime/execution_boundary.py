from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from app.config.settings import AppSettings
from app.connectors.filesystem import FilesystemConnector
from app.connectors.http import HttpConnector
from app.connectors.outlook import OutlookConnector
from app.connectors.system import SystemConnector
from app.connectors.task import TaskConnector
from app.core.database import Database
from app.core.errors import ConnectorError
from app.core.utils import json_dumps, sha256_hex
from app.policy.path_guard import PathGuard
from app.schemas.actions import ExecutionBoundaryMetadata, ExecutionBundle
from app.schemas.settings import EffectiveSettings
from app.services.agent_workspace_service import AgentWorkspaceService


class _StaticSettingsService:
    def __init__(self, settings: EffectiveSettings):
        self._settings = settings

    def get_effective_settings(self) -> EffectiveSettings:
        return self._settings


class ExecutionBoundaryBackend(Protocol):
    name: str
    isolation_label: str

    def execute(
        self,
        runner: "ConstrainedExecutionRunner",
        bundle: ExecutionBundle,
        *,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        ...


class SubprocessExecutionBackend:
    name = "subprocess-json-bundle"
    isolation_label = "child-process / env-scrubbed / same-user"

    def execute(
        self,
        runner: "ConstrainedExecutionRunner",
        bundle: ExecutionBundle,
        *,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return runner._run_subprocess(bundle, heartbeat_callback=heartbeat_callback)


class TaskActionBroker:
    def __init__(self, base_settings: AppSettings):
        self.connector = TaskConnector(Database(base_settings.resolved_database_path))

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.connector.execute(action_type, payload)


class ConstrainedExecutionRunner:
    SAFE_ENV_KEYS = [
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    ]
    MIN_CHILD_HARD_TIMEOUT_SECONDS = 60
    CHILD_HARD_TIMEOUT_MULTIPLIER = 4

    def __init__(self, base_settings: AppSettings, backend: ExecutionBoundaryBackend | None = None):
        self.base_settings = base_settings
        self.backend = backend or SubprocessExecutionBackend()
        self.task_broker = TaskActionBroker(base_settings)
        self.agent_workspace_service = AgentWorkspaceService(base_settings)

    def build_bundle(
        self,
        *,
        proposal,
        approval_id: str,
        manifest_hash: str,
        runtime_payload: dict[str, Any],
        allowed_connectors: list[str],
        capabilities: list[str],
        effective_settings: EffectiveSettings,
    ) -> ExecutionBundle:
        exact_file_paths = self._exact_file_scope(
            connector=proposal.connector,
            action_type=proposal.action_type,
            runtime_payload=runtime_payload,
            effective_settings=effective_settings,
        )
        exact_http_targets = self._exact_http_scope(
            connector=proposal.connector,
            action_type=proposal.action_type,
            runtime_payload=runtime_payload,
        )
        child_http_scope = self._narrow_http_settings(
            exact_http_targets=exact_http_targets,
            effective_settings=effective_settings,
        )
        executor_layout = self.agent_workspace_service.layout_for(
            run_id=proposal.run_id,
            agent_role="executor",
            memory_namespace="executor",
            context_namespace=f"executor:{proposal.run_id}:{proposal.connector}",
            branch_key=proposal.connector,
        )
        child_filesystem_roots = exact_file_paths or list(effective_settings.allowed_filesystem_roots)
        database_access = "brokered-task-actions" if proposal.connector == "task" else "none"
        boundary = ExecutionBoundaryMetadata(
            mode=self.backend.name,
            isolation_level=self.backend.isolation_label,
            backend=self.backend.name,
            environment_scrubbed=True,
            allowed_environment_keys=self._allowed_environment_keys(),
            secrets_access="denied",
            database_access=database_access,
            filesystem_scope=child_filesystem_roots,
            network_scope=exact_http_targets or child_http_scope["allowed_http_hosts"],
            granted_file_paths=exact_file_paths,
            granted_http_targets=exact_http_targets,
            capability_tokens=sorted(set(capabilities)),
            scope_strategy="exact-task-scope" if (exact_file_paths or exact_http_targets) else "connector-bounded",
            cwd=str(executor_layout.scratch_root),
            python_executable=sys.executable,
            shared_workspace_root=str(executor_layout.shared_workspace_root),
            agent_work_root=str(executor_layout.agent_root),
            agent_scratch_root=str(executor_layout.scratch_root),
            promotion_root=str(executor_layout.promotion_root),
            notes=[
                "Approved actions execute in a dedicated child Python process with a scrubbed environment.",
                "The child interpreter runs with explicit import bootstrap instead of inheriting ambient PYTHONPATH.",
                "This boundary reduces control-plane coupling but is not an OS or kernel sandbox.",
                "Connector access is restricted to the single approved action bundle and brokered task actions.",
                "The child process starts inside an executor-specific scratch area and does not share a writable scratch directory with planner, reviewer, or reporter agents.",
            ],
        )
        return ExecutionBundle(
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            connector=proposal.connector,
            action_type=proposal.action_type,
            payload=runtime_payload,
            manifest_hash=manifest_hash,
            approval_id=approval_id,
            correlation_id=proposal.correlation_id,
            allowed_connectors=allowed_connectors,
            capabilities=capabilities,
            execution_settings={
                "runtime_mode": effective_settings.runtime_mode.value,
                "runtime_state_root": effective_settings.runtime_state_root,
                "workspace_root": effective_settings.workspace_root,
                "database_path": str(self.base_settings.resolved_runtime_state_root / "data" / "child_boundary_unused.db"),
                "allowed_filesystem_roots": child_filesystem_roots,
                "allowed_http_hosts": child_http_scope["allowed_http_hosts"],
                "allowed_http_schemes": child_http_scope["allowed_http_schemes"],
                "allowed_http_ports": child_http_scope["allowed_http_ports"],
                "allow_http_private_network": effective_settings.allow_http_private_network,
                "http_follow_redirects": effective_settings.http_follow_redirects,
                "http_timeout_seconds": effective_settings.http_timeout_seconds,
                "http_max_response_bytes": effective_settings.http_max_response_bytes,
                "filesystem_max_read_bytes": effective_settings.filesystem_max_read_bytes,
                "enable_system_connector": effective_settings.enable_system_connector,
                "enable_outlook_connector": effective_settings.enable_outlook_connector,
                "allowed_file_paths": exact_file_paths,
                "allowed_http_targets": exact_http_targets,
                "boundary_backend": self.backend.name,
                "agent_work_root": str(executor_layout.agent_root),
                "agent_scratch_root": str(executor_layout.scratch_root),
                "agent_reports_root": str(executor_layout.reports_root),
                "promotion_root": str(executor_layout.promotion_root),
                "shared_workspace_root": str(executor_layout.shared_workspace_root),
                "child_cwd": str(executor_layout.scratch_root),
            },
            boundary=boundary,
        )

    @staticmethod
    def bundle_hash(bundle: ExecutionBundle) -> str:
        return sha256_hex(json_dumps(bundle.model_dump(mode="json")))

    def execute(
        self,
        bundle: ExecutionBundle,
        *,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], ExecutionBoundaryMetadata]:
        payload = self.backend.execute(self, bundle, heartbeat_callback=heartbeat_callback)
        boundary_metadata = ExecutionBoundaryMetadata(**payload.get("boundary_metadata", bundle.boundary.model_dump()))
        if payload.get("broker_request"):
            result = self._execute_broker_request(payload["broker_request"])
            return result, boundary_metadata
        if payload.get("ok"):
            return payload.get("result") or {}, boundary_metadata
        error_message = payload.get("error") or "Child execution boundary failed."
        raise ConnectorError(error_message)

    def _run_subprocess(
        self,
        bundle: ExecutionBundle,
        *,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        soft_timeout_seconds, hard_timeout_seconds = self._child_timeout_limits()
        process = subprocess.Popen(
            self._child_command(),
            cwd=str(bundle.execution_settings.get("child_cwd") or self.base_settings.resolved_runtime_state_root),
            env=self._scrubbed_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        heartbeat_interval = max(1, min(5, soft_timeout_seconds // 3 or 1))
        start = time.monotonic()
        stdout = ""
        stderr = ""
        pending_input = bundle.model_dump_json()
        while True:
            try:
                stdout, stderr = process.communicate(input=pending_input, timeout=heartbeat_interval)
                pending_input = None
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                if heartbeat_callback is not None:
                    heartbeat_callback()
                if time.monotonic() - start > hard_timeout_seconds:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise ConnectorError(
                        f"Execution boundary timed out after {hard_timeout_seconds} seconds."
                        + (f" stderr={(stderr or '').strip()}" if stderr else "")
                    )
        stdout = (stdout or "").strip()
        if not stdout:
            stderr = (stderr or "").strip()
            raise ConnectorError(
                "Execution boundary returned no JSON payload."
                + (f" stderr={stderr}" if stderr else "")
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ConnectorError(f"Execution boundary returned invalid JSON: {exc}") from exc

    def _scrubbed_environment(self) -> dict[str, str]:
        env = {
            key: value
            for key in self.SAFE_ENV_KEYS
            if (value := os.environ.get(key))
        }
        return env

    def _allowed_environment_keys(self) -> list[str]:
        return sorted(key for key in self.SAFE_ENV_KEYS if os.environ.get(key))

    def _child_timeout_limits(self) -> tuple[int, int]:
        soft_timeout = max(5, int(self.base_settings.worker_lease_seconds))
        hard_timeout = max(
            self.MIN_CHILD_HARD_TIMEOUT_SECONDS,
            soft_timeout * self.CHILD_HARD_TIMEOUT_MULTIPLIER,
        )
        return soft_timeout, hard_timeout

    def _child_command(self) -> list[str]:
        bootstrap = self._child_bootstrap_code()
        return [sys.executable, "-I", "-S", "-c", bootstrap]

    def _child_bootstrap_code(self) -> str:
        project_root = json.dumps(str(self.base_settings.project_root))
        import_paths = json.dumps(self._child_import_paths())
        return "\n".join(
            [
                "import runpy",
                "import sys",
                f"project_root = {project_root}",
                f"paths = {import_paths}",
                "for path in reversed(paths):",
                "    if path not in sys.path:",
                "        sys.path.insert(0, path)",
                "if project_root not in sys.path:",
                "    sys.path.insert(0, project_root)",
                "runpy.run_module('app.runtime.child_process', run_name='__main__')",
            ]
        )

    def _child_import_paths(self) -> list[str]:
        project_root = self.base_settings.project_root.resolve()
        dependency_roots: list[str] = []
        executable = Path(sys.executable).resolve()
        candidates = [
            executable.parent.parent / "Lib" / "site-packages",
            executable.parent.parent / "lib" / "site-packages",
        ]
        for candidate in candidates:
            if candidate.exists():
                resolved = str(candidate.resolve())
                if resolved not in dependency_roots:
                    dependency_roots.append(resolved)

        for entry in sys.path:
            if not entry:
                continue
            try:
                resolved_path = Path(entry).resolve()
            except OSError:
                continue
            if not resolved_path.exists() or not resolved_path.is_dir():
                continue
            if resolved_path == project_root:
                continue
            normalized = resolved_path.as_posix().lower()
            if "site-packages" not in normalized and not normalized.endswith("/.codex-pkgs"):
                continue
            resolved = str(resolved_path)
            if resolved not in dependency_roots:
                dependency_roots.append(resolved)
        return dependency_roots

    @staticmethod
    def _narrow_http_settings(
        *,
        exact_http_targets: list[str],
        effective_settings: EffectiveSettings,
    ) -> dict[str, list[Any]]:
        if not exact_http_targets:
            return {
                "allowed_http_hosts": list(effective_settings.allowed_http_hosts),
                "allowed_http_schemes": list(effective_settings.allowed_http_schemes),
                "allowed_http_ports": list(effective_settings.allowed_http_ports),
            }
        hosts: list[str] = []
        schemes: list[str] = []
        ports: list[int] = []
        for target in exact_http_targets:
            _, _, raw_url = target.partition(" ")
            parsed = urlsplit(raw_url)
            if parsed.hostname and parsed.hostname not in hosts:
                hosts.append(parsed.hostname)
            if parsed.scheme and parsed.scheme not in schemes:
                schemes.append(parsed.scheme)
            port = parsed.port
            if port is None:
                port = 443 if parsed.scheme == "https" else 80
            if port not in ports:
                ports.append(port)
        return {
            "allowed_http_hosts": hosts,
            "allowed_http_schemes": schemes,
            "allowed_http_ports": ports,
        }

    def _execute_broker_request(self, broker_request: dict[str, Any]) -> dict[str, Any]:
        connector = broker_request.get("connector")
        if connector != "task":
            raise ConnectorError(f"Unsupported brokered connector request: {connector}")
        return self.task_broker.execute(
            broker_request.get("action_type", ""),
            broker_request.get("payload") or {},
        )

    def _exact_file_scope(
        self,
        *,
        connector: str,
        action_type: str,
        runtime_payload: dict[str, Any],
        effective_settings: EffectiveSettings,
    ) -> list[str]:
        if connector not in {"filesystem", "system"}:
            return []
        static_settings = _StaticSettingsService(effective_settings)
        path_guard = PathGuard(self.base_settings, static_settings)
        scoped: list[str] = []
        for key in ("path", "source_path", "destination_path"):
            raw_path = runtime_payload.get(key)
            if not raw_path:
                continue
            resolver = self._path_resolver_for(action_type, key, connector)
            try:
                resolved = resolver(path_guard, raw_path)
            except Exception:
                continue
            resolved_str = str(resolved)
            if resolved_str not in scoped:
                scoped.append(resolved_str)
        return scoped

    @staticmethod
    def _path_resolver_for(action_type: str, key: str, connector: str):
        if connector == "system":
            if action_type == "system.test_path":
                return lambda guard, raw_path: guard.resolve_for_probe(raw_path)
            return lambda guard, raw_path: guard.resolve_for_read(raw_path)
        if key == "destination_path":
            return lambda guard, raw_path: guard.resolve_for_write(raw_path)
        if action_type in {
            "filesystem.write_text",
            "filesystem.append_text",
            "filesystem.delete_path",
            "filesystem.make_directory",
            "filesystem.move_path",
        }:
            return lambda guard, raw_path: guard.resolve_for_write(raw_path)
        if action_type == "filesystem.copy_path" and key == "source_path":
            return lambda guard, raw_path: guard.resolve_for_read(raw_path)
        if action_type in {"filesystem.list_directory", "filesystem.read_text"}:
            return lambda guard, raw_path: guard.resolve_for_read(raw_path)
        return lambda guard, raw_path: guard.resolve_for_probe(raw_path)

    @staticmethod
    def _exact_http_scope(
        *,
        connector: str,
        action_type: str,
        runtime_payload: dict[str, Any],
    ) -> list[str]:
        if connector != "http":
            return []
        url = runtime_payload.get("url")
        if not url:
            return []
        method = action_type.split(".", 1)[1].upper() if "." in action_type else "GET"
        parsed = urlsplit(url)
        target = f"{method} {parsed.scheme}://{parsed.netloc}{parsed.path}"
        return [target]


def run_bundle_in_child(bundle: ExecutionBundle) -> dict[str, Any]:
    boundary_metadata = bundle.boundary.model_copy(
        update={
            "cwd": os.getcwd(),
            "python_executable": sys.executable,
        }
    )
    try:
        if bundle.connector == "task":
            return {
                "ok": True,
                "broker_request": {
                    "connector": bundle.connector,
                    "action_type": bundle.action_type,
                    "payload": bundle.payload,
                },
                "boundary_metadata": boundary_metadata.model_dump(mode="json"),
            }
        _enforce_exact_scope(bundle)
        connector = _build_child_connector(bundle)
        result = connector.execute(bundle.action_type, bundle.payload)
        return {
            "ok": True,
            "result": result,
            "boundary_metadata": boundary_metadata.model_dump(mode="json"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "boundary_metadata": boundary_metadata.model_dump(mode="json"),
        }


def _build_child_connector(bundle: ExecutionBundle):
    if "execute-approved-action" not in bundle.capabilities:
        raise ConnectorError("Execution bundle is missing executor capability.")
    if bundle.connector not in bundle.allowed_connectors:
        raise ConnectorError(f"Connector {bundle.connector} is not allowed by the execution bundle.")
    settings = _settings_from_bundle(bundle)
    static_settings = _StaticSettingsService(settings)
    base_settings = _base_settings_from_bundle(bundle)
    if bundle.connector == "filesystem":
        return FilesystemConnector(base_settings, static_settings)
    if bundle.connector == "http":
        return HttpConnector(static_settings)
    if bundle.connector == "system":
        return SystemConnector(base_settings, static_settings)
    if bundle.connector == "outlook" and settings.enable_outlook_connector:
        return OutlookConnector()
    raise ConnectorError(f"Connector {bundle.connector} is not available inside the child execution boundary.")


def _enforce_exact_scope(bundle: ExecutionBundle) -> None:
    if bundle.connector in {"filesystem", "system"}:
        allowed_paths = set(bundle.execution_settings.get("allowed_file_paths") or [])
        for key in ("path", "source_path", "destination_path"):
            raw_path = bundle.payload.get(key)
            if not raw_path:
                continue
            resolved = _resolve_bundle_path(bundle, raw_path)
            if allowed_paths and resolved not in allowed_paths:
                raise ConnectorError(f"Exact path scope denied for {resolved}.")
    if bundle.connector == "http":
        allowed_targets = set(bundle.execution_settings.get("allowed_http_targets") or [])
        url = bundle.payload.get("url")
        if url and allowed_targets:
            method = bundle.action_type.split(".", 1)[1].upper() if "." in bundle.action_type else "GET"
            parsed = urlsplit(url)
            target = f"{method} {parsed.scheme}://{parsed.netloc}{parsed.path}"
            if target not in allowed_targets:
                raise ConnectorError(f"Exact HTTP scope denied for {target}.")


def _resolve_bundle_path(bundle: ExecutionBundle, raw_path: str) -> str:
    candidate = Path(str(raw_path))
    if not candidate.is_absolute():
        candidate = Path(bundle.execution_settings["workspace_root"]) / candidate
    return str(candidate.resolve(strict=False))


def _base_settings_from_bundle(bundle: ExecutionBundle) -> AppSettings:
    execution_settings = bundle.execution_settings
    return AppSettings(
        _env_file=None,
        app_name="Win Agent Runtime Child Execution",
        runtime_state_root=Path(execution_settings["runtime_state_root"]),
        workspace_root=Path(execution_settings["workspace_root"]),
        database_path=Path(execution_settings["database_path"]),
        audit_log_path=Path("child-boundary-audit.jsonl"),
        allowed_filesystem_roots=",".join(execution_settings["allowed_filesystem_roots"]),
        allowed_http_hosts=",".join(execution_settings["allowed_http_hosts"]),
        allowed_http_schemes=",".join(execution_settings["allowed_http_schemes"]),
        allowed_http_ports=",".join(str(port) for port in execution_settings["allowed_http_ports"]),
        allow_http_private_network=bool(execution_settings["allow_http_private_network"]),
        http_follow_redirects=bool(execution_settings["http_follow_redirects"]),
        http_timeout_seconds=float(execution_settings["http_timeout_seconds"]),
        http_max_response_bytes=int(execution_settings["http_max_response_bytes"]),
        filesystem_max_read_bytes=int(execution_settings["filesystem_max_read_bytes"]),
        enable_system_connector=bool(execution_settings["enable_system_connector"]),
        enable_outlook_connector=bool(execution_settings["enable_outlook_connector"]),
        session_secret="child-execution-boundary",
    )


def _settings_from_bundle(bundle: ExecutionBundle) -> EffectiveSettings:
    execution_settings = bundle.execution_settings
    return EffectiveSettings(
        app_name="Win Agent Runtime Child Execution",
        runtime_mode=execution_settings["runtime_mode"],
        runtime_state_root=execution_settings["runtime_state_root"],
        data_dir=str(Path(execution_settings["runtime_state_root"]) / "data"),
        secrets_dir=str(Path(execution_settings["runtime_state_root"]) / "secrets"),
        logs_dir=str(Path(execution_settings["runtime_state_root"]) / "logs"),
        workspace_root=execution_settings["workspace_root"],
        provider="mock",
        fallback_provider="mock",
        model="local-execution",
        base_url=None,
        api_key_env="CHILD_BOUNDARY_UNUSED",
        generic_http_endpoint=None,
        provider_timeout_seconds=30.0,
        provider_max_retries=0,
        provider_circuit_breaker_threshold=1,
        provider_circuit_breaker_seconds=1,
        provider_configs=[],
        summary_profile="local-only",
        planning_profile="local-only",
        fast_provider="mock",
        cheap_provider="mock",
        strong_provider="mock",
        local_provider="mock",
        privacy_provider="mock",
        provider_allowed_hosts=[],
        allow_provider_private_network=False,
        allow_restricted_provider_egress=False,
        json_audit_enabled=False,
        session_max_age_seconds=300,
        session_idle_timeout_seconds=300,
        recent_auth_window_seconds=60,
        max_request_size_bytes=1_048_576,
        trusted_hosts=["127.0.0.1", "localhost"],
        allowed_http_schemes=list(execution_settings["allowed_http_schemes"]),
        allowed_http_ports=list(execution_settings["allowed_http_ports"]),
        allow_http_private_network=bool(execution_settings["allow_http_private_network"]),
        http_follow_redirects=bool(execution_settings["http_follow_redirects"]),
        http_timeout_seconds=float(execution_settings["http_timeout_seconds"]),
        http_max_response_bytes=int(execution_settings["http_max_response_bytes"]),
        filesystem_max_read_bytes=int(execution_settings["filesystem_max_read_bytes"]),
        allowed_filesystem_roots=list(execution_settings["allowed_filesystem_roots"]),
        allowed_http_hosts=list(execution_settings["allowed_http_hosts"]),
        enable_outlook_connector=bool(execution_settings["enable_outlook_connector"]),
        enable_system_connector=bool(execution_settings["enable_system_connector"]),
        configured_secret_envs=[],
        cli_auth_mode="child-boundary",
        local_protection_mode="dpapi",
        allow_insecure_local_storage=False,
        history_retention_days=1,
        cli_token_ttl_seconds=60,
        worker_lease_seconds=30,
        worker_max_attempts=1,
    )
