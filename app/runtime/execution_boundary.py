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

    def __init__(self, base_settings: AppSettings, backend: ExecutionBoundaryBackend | None = None):
        self.base_settings = base_settings
        self.backend = backend or SubprocessExecutionBackend()
        self.task_broker = TaskActionBroker(base_settings)

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
        database_access = "brokered-task-actions" if proposal.connector == "task" else "none"
        boundary = ExecutionBoundaryMetadata(
            mode=self.backend.name,
            isolation_level=self.backend.isolation_label,
            backend=self.backend.name,
            environment_scrubbed=True,
            allowed_environment_keys=self._allowed_environment_keys(),
            secrets_access="denied",
            database_access=database_access,
            filesystem_scope=exact_file_paths or list(effective_settings.allowed_filesystem_roots),
            network_scope=exact_http_targets or list(effective_settings.allowed_http_hosts),
            granted_file_paths=exact_file_paths,
            granted_http_targets=exact_http_targets,
            capability_tokens=sorted(set(capabilities)),
            scope_strategy="exact-task-scope" if (exact_file_paths or exact_http_targets) else "connector-bounded",
            cwd=str(self.base_settings.project_root),
            python_executable=sys.executable,
            notes=[
                "Approved actions execute in a dedicated child Python process with a scrubbed environment.",
                "This boundary reduces control-plane coupling but is not an OS or kernel sandbox.",
                "Connector access is restricted to the single approved action bundle and brokered task actions.",
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
                "allowed_filesystem_roots": effective_settings.allowed_filesystem_roots,
                "allowed_http_hosts": effective_settings.allowed_http_hosts,
                "allowed_http_schemes": effective_settings.allowed_http_schemes,
                "allowed_http_ports": effective_settings.allowed_http_ports,
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
        process = subprocess.Popen(
            [sys.executable, "-m", "app.runtime.child_process"],
            cwd=str(self.base_settings.project_root),
            env=self._scrubbed_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        timeout_seconds = max(5, int(self.base_settings.worker_lease_seconds))
        heartbeat_interval = max(1, min(5, timeout_seconds // 3 or 1))
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
                if time.monotonic() - start > timeout_seconds:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise ConnectorError(
                        "Execution boundary timed out."
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
        env["PYTHONIOENCODING"] = "utf-8"
        python_paths = [str(self.base_settings.project_root)]
        codex_packages = self.base_settings.project_root / ".codex-pkgs"
        if codex_packages.exists():
            python_paths.append(str(codex_packages))
        if os.environ.get("PYTHONPATH"):
            python_paths.append(os.environ["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
        return env

    def _allowed_environment_keys(self) -> list[str]:
        keys = [key for key in self.SAFE_ENV_KEYS if os.environ.get(key)]
        keys.extend(["PYTHONIOENCODING", "PYTHONPATH"])
        return sorted(set(keys))

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
    connectors = {
        "filesystem": FilesystemConnector(base_settings, static_settings),
        "http": HttpConnector(static_settings),
        "system": SystemConnector(base_settings, static_settings),
    }
    if settings.enable_outlook_connector:
        connectors["outlook"] = OutlookConnector()
    connector = connectors.get(bundle.connector)
    if connector is None:
        raise ConnectorError(f"Connector {bundle.connector} is not available inside the child execution boundary.")
    return connector


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
