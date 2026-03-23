from __future__ import annotations

import os
from typing import Any

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.utils import json_dumps, new_id, sha256_hex, split_csv, utcnow_iso
from app.schemas.actions import RuntimeMode
from app.schemas.settings import EffectiveSettings, SanitizedSettingsExport, SettingsUpdate


class SettingsService:
    def __init__(self, base_settings: AppSettings, database: Database):
        self.base_settings = base_settings
        self.database = database

    def get_effective_settings(self) -> EffectiveSettings:
        overrides = self._load_override_map()
        runtime_mode = RuntimeMode(overrides.get("runtime_mode", self.base_settings.runtime_mode))
        configured_secret_envs = [
            env_name
            for env_name in {
                overrides.get("api_key_env", self.base_settings.api_key_env),
                self.base_settings.anthropic_api_key_env,
                self.base_settings.gemini_api_key_env,
            }
            if env_name and os.getenv(env_name)
        ]
        return EffectiveSettings(
            app_name=self.base_settings.app_name,
            runtime_mode=runtime_mode,
            runtime_state_root=str(self.base_settings.resolved_runtime_state_root),
            data_dir=str(self.base_settings.resolved_data_dir),
            secrets_dir=str(self.base_settings.resolved_secrets_dir),
            logs_dir=str(self.base_settings.resolved_logs_dir),
            workspace_root=str(self.base_settings.resolved_workspace_root),
            provider=overrides.get("provider", self.base_settings.provider),
            fallback_provider=overrides.get("fallback_provider", self.base_settings.fallback_provider),
            model=overrides.get("model", self.base_settings.model),
            base_url=overrides.get("base_url", self.base_settings.base_url),
            api_key_env=overrides.get("api_key_env", self.base_settings.api_key_env),
            generic_http_endpoint=overrides.get(
                "generic_http_endpoint",
                self.base_settings.generic_http_endpoint,
            ),
            provider_timeout_seconds=float(
                overrides.get("provider_timeout_seconds", self.base_settings.provider_timeout_seconds)
            ),
            provider_max_retries=int(
                overrides.get("provider_max_retries", self.base_settings.provider_max_retries)
            ),
            provider_circuit_breaker_threshold=int(
                overrides.get(
                    "provider_circuit_breaker_threshold",
                    self.base_settings.provider_circuit_breaker_threshold,
                )
            ),
            provider_circuit_breaker_seconds=int(
                overrides.get(
                    "provider_circuit_breaker_seconds",
                    self.base_settings.provider_circuit_breaker_seconds,
                )
            ),
            summary_profile=overrides.get("summary_profile", self.base_settings.summary_profile),
            planning_profile=overrides.get("planning_profile", self.base_settings.planning_profile),
            fast_provider=overrides.get("fast_provider", self.base_settings.fast_provider),
            cheap_provider=overrides.get("cheap_provider", self.base_settings.cheap_provider),
            strong_provider=overrides.get("strong_provider", self.base_settings.strong_provider),
            local_provider=overrides.get("local_provider", self.base_settings.local_provider),
            privacy_provider=overrides.get("privacy_provider", self.base_settings.privacy_provider),
            provider_allowed_hosts=split_csv(
                overrides.get("provider_allowed_hosts", self.base_settings.provider_allowed_hosts)
            ),
            allow_provider_private_network=self._parse_bool(
                overrides.get(
                    "allow_provider_private_network",
                    self.base_settings.allow_provider_private_network,
                )
            ),
            allow_restricted_provider_egress=self._parse_bool(
                overrides.get(
                    "allow_restricted_provider_egress",
                    self.base_settings.allow_restricted_provider_egress,
                )
            ),
            json_audit_enabled=self._parse_bool(
                overrides.get("json_audit_enabled", self.base_settings.json_audit_enabled)
            ),
            session_max_age_seconds=int(
                overrides.get("session_max_age_seconds", self.base_settings.session_max_age_seconds)
            ),
            session_idle_timeout_seconds=int(
                overrides.get(
                    "session_idle_timeout_seconds",
                    self.base_settings.session_idle_timeout_seconds,
                )
            ),
            recent_auth_window_seconds=int(
                overrides.get("recent_auth_window_seconds", self.base_settings.recent_auth_window_seconds)
            ),
            max_request_size_bytes=int(
                overrides.get("max_request_size_bytes", self.base_settings.max_request_size_bytes)
            ),
            trusted_hosts=split_csv(overrides.get("trusted_hosts", self.base_settings.trusted_hosts)),
            allowed_http_schemes=split_csv(
                overrides.get("allowed_http_schemes", self.base_settings.allowed_http_schemes)
            ),
            allowed_http_ports=self._parse_int_list(
                overrides.get("allowed_http_ports", self.base_settings.allowed_http_ports)
            ),
            allow_http_private_network=self._parse_bool(
                overrides.get("allow_http_private_network", self.base_settings.allow_http_private_network)
            ),
            http_follow_redirects=self._parse_bool(
                overrides.get("http_follow_redirects", self.base_settings.http_follow_redirects)
            ),
            http_timeout_seconds=float(
                overrides.get("http_timeout_seconds", self.base_settings.http_timeout_seconds)
            ),
            http_max_response_bytes=int(
                overrides.get("http_max_response_bytes", self.base_settings.http_max_response_bytes)
            ),
            filesystem_max_read_bytes=int(
                overrides.get("filesystem_max_read_bytes", self.base_settings.filesystem_max_read_bytes)
            ),
            allowed_filesystem_roots=split_csv(
                overrides.get("allowed_filesystem_roots", self.base_settings.allowed_filesystem_roots)
            ),
            allowed_http_hosts=split_csv(
                overrides.get("allowed_http_hosts", self.base_settings.allowed_http_hosts)
            ),
            enable_outlook_connector=self._parse_bool(
                overrides.get("enable_outlook_connector", self.base_settings.enable_outlook_connector)
            ),
            enable_system_connector=self._parse_bool(
                overrides.get("enable_system_connector", self.base_settings.enable_system_connector)
            ),
            configured_secret_envs=sorted(configured_secret_envs),
            cli_auth_mode="short-lived-password-issued",
            local_protection_mode=overrides.get("local_protection_mode", self.base_settings.local_protection_mode),
            history_retention_days=int(
                overrides.get("history_retention_days", self.base_settings.history_retention_days)
            ),
            cli_token_ttl_seconds=int(
                overrides.get("cli_token_ttl_seconds", self.base_settings.cli_token_ttl_seconds)
            ),
            worker_lease_seconds=int(
                overrides.get("worker_lease_seconds", self.base_settings.worker_lease_seconds)
            ),
            worker_max_attempts=int(
                overrides.get("worker_max_attempts", self.base_settings.worker_max_attempts)
            ),
        )

    def save(
        self,
        update: SettingsUpdate,
        actor: str = "system",
        reason: str | None = None,
    ) -> EffectiveSettings:
        payload: dict[str, Any] = update.model_dump()
        updated_at = utcnow_iso()
        for key, value in payload.items():
            if value is None:
                continue
            serialized = value.value if hasattr(value, "value") else value
            self.database.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(serialized), updated_at),
            )
        effective = self.get_effective_settings()
        export = self.export_sanitized()
        self.database.execute(
            """
            INSERT INTO settings_versions(id, settings_hash, settings_json, actor, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("settings"),
                self.hash_export(export),
                json_dumps(export.model_dump(mode="json")),
                actor,
                reason,
                updated_at,
            ),
        )
        return effective

    def list_raw_settings(self) -> dict[str, str]:
        return self._load_override_map()

    def export_sanitized(self) -> SanitizedSettingsExport:
        settings = self.get_effective_settings()
        return SanitizedSettingsExport(
            runtime_mode=settings.runtime_mode,
            workspace_root=settings.workspace_root,
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
            provider_allowed_hosts=settings.provider_allowed_hosts,
            allow_provider_private_network=settings.allow_provider_private_network,
            allow_restricted_provider_egress=settings.allow_restricted_provider_egress,
            json_audit_enabled=settings.json_audit_enabled,
            session_max_age_seconds=settings.session_max_age_seconds,
            session_idle_timeout_seconds=settings.session_idle_timeout_seconds,
            recent_auth_window_seconds=settings.recent_auth_window_seconds,
            max_request_size_bytes=settings.max_request_size_bytes,
            allowed_http_schemes=settings.allowed_http_schemes,
            allowed_http_ports=settings.allowed_http_ports,
            allow_http_private_network=settings.allow_http_private_network,
            http_follow_redirects=settings.http_follow_redirects,
            http_timeout_seconds=settings.http_timeout_seconds,
            http_max_response_bytes=settings.http_max_response_bytes,
            filesystem_max_read_bytes=settings.filesystem_max_read_bytes,
            allowed_filesystem_roots=settings.allowed_filesystem_roots,
            allowed_http_hosts=settings.allowed_http_hosts,
            enable_system_connector=settings.enable_system_connector,
            enable_outlook_connector=settings.enable_outlook_connector,
            local_protection_mode=settings.local_protection_mode,
            history_retention_days=settings.history_retention_days,
            cli_token_ttl_seconds=settings.cli_token_ttl_seconds,
            worker_lease_seconds=settings.worker_lease_seconds,
            worker_max_attempts=settings.worker_max_attempts,
        )

    def import_sanitized(
        self,
        exported: SanitizedSettingsExport,
        actor: str = "system",
        reason: str | None = None,
    ) -> EffectiveSettings:
        return self.save(
            SettingsUpdate(
                runtime_mode=exported.runtime_mode,
                provider=exported.provider,
                fallback_provider=exported.fallback_provider,
                model=exported.model,
                base_url=exported.base_url,
                api_key_env=exported.api_key_env,
                generic_http_endpoint=exported.generic_http_endpoint,
                provider_timeout_seconds=exported.provider_timeout_seconds,
                provider_max_retries=exported.provider_max_retries,
                provider_circuit_breaker_threshold=exported.provider_circuit_breaker_threshold,
                provider_circuit_breaker_seconds=exported.provider_circuit_breaker_seconds,
                summary_profile=exported.summary_profile,
                planning_profile=exported.planning_profile,
                fast_provider=exported.fast_provider,
                cheap_provider=exported.cheap_provider,
                strong_provider=exported.strong_provider,
                local_provider=exported.local_provider,
                privacy_provider=exported.privacy_provider,
                provider_allowed_hosts=",".join(exported.provider_allowed_hosts),
                allow_provider_private_network=exported.allow_provider_private_network,
                allow_restricted_provider_egress=exported.allow_restricted_provider_egress,
                json_audit_enabled=exported.json_audit_enabled,
                session_max_age_seconds=exported.session_max_age_seconds,
                session_idle_timeout_seconds=exported.session_idle_timeout_seconds,
                recent_auth_window_seconds=exported.recent_auth_window_seconds,
                max_request_size_bytes=exported.max_request_size_bytes,
                allowed_http_schemes=",".join(exported.allowed_http_schemes),
                allowed_http_ports=",".join(str(port) for port in exported.allowed_http_ports),
                allow_http_private_network=exported.allow_http_private_network,
                http_follow_redirects=exported.http_follow_redirects,
                http_timeout_seconds=exported.http_timeout_seconds,
                http_max_response_bytes=exported.http_max_response_bytes,
                filesystem_max_read_bytes=exported.filesystem_max_read_bytes,
                allowed_filesystem_roots=",".join(exported.allowed_filesystem_roots),
                allowed_http_hosts=",".join(exported.allowed_http_hosts),
                enable_system_connector=exported.enable_system_connector,
                enable_outlook_connector=exported.enable_outlook_connector,
                local_protection_mode=exported.local_protection_mode,
                history_retention_days=exported.history_retention_days,
                cli_token_ttl_seconds=exported.cli_token_ttl_seconds,
                worker_lease_seconds=exported.worker_lease_seconds,
                worker_max_attempts=exported.worker_max_attempts,
            ),
            actor=actor,
            reason=reason,
        )

    def reset_to_safe_defaults(
        self,
        actor: str = "system",
        reason: str | None = None,
    ) -> EffectiveSettings:
        return self.save(
            SettingsUpdate(
                runtime_mode=RuntimeMode.SAFE,
                provider=self.base_settings.provider,
                fallback_provider=self.base_settings.fallback_provider,
                model=self.base_settings.model,
                base_url=self.base_settings.base_url,
                api_key_env=self.base_settings.api_key_env,
                generic_http_endpoint=self.base_settings.generic_http_endpoint,
                provider_timeout_seconds=self.base_settings.provider_timeout_seconds,
                provider_max_retries=self.base_settings.provider_max_retries,
                provider_circuit_breaker_threshold=self.base_settings.provider_circuit_breaker_threshold,
                provider_circuit_breaker_seconds=self.base_settings.provider_circuit_breaker_seconds,
                summary_profile=self.base_settings.summary_profile,
                planning_profile=self.base_settings.planning_profile,
                fast_provider=self.base_settings.fast_provider,
                cheap_provider=self.base_settings.cheap_provider,
                strong_provider=self.base_settings.strong_provider,
                local_provider=self.base_settings.local_provider,
                privacy_provider=self.base_settings.privacy_provider,
                provider_allowed_hosts=self.base_settings.provider_allowed_hosts,
                allow_provider_private_network=self.base_settings.allow_provider_private_network,
                allow_restricted_provider_egress=False,
                json_audit_enabled=True,
                session_max_age_seconds=self.base_settings.session_max_age_seconds,
                session_idle_timeout_seconds=self.base_settings.session_idle_timeout_seconds,
                recent_auth_window_seconds=self.base_settings.recent_auth_window_seconds,
                max_request_size_bytes=self.base_settings.max_request_size_bytes,
                allowed_http_schemes=self.base_settings.allowed_http_schemes,
                allowed_http_ports=self.base_settings.allowed_http_ports,
                allow_http_private_network=False,
                http_follow_redirects=False,
                http_timeout_seconds=self.base_settings.http_timeout_seconds,
                http_max_response_bytes=self.base_settings.http_max_response_bytes,
                filesystem_max_read_bytes=self.base_settings.filesystem_max_read_bytes,
                allowed_filesystem_roots=self.base_settings.allowed_filesystem_roots,
                allowed_http_hosts=self.base_settings.allowed_http_hosts,
                enable_system_connector=self.base_settings.enable_system_connector,
                enable_outlook_connector=False,
                local_protection_mode=self.base_settings.local_protection_mode,
                history_retention_days=self.base_settings.history_retention_days,
                cli_token_ttl_seconds=self.base_settings.cli_token_ttl_seconds,
                worker_lease_seconds=self.base_settings.worker_lease_seconds,
                worker_max_attempts=self.base_settings.worker_max_attempts,
            ),
            actor=actor,
            reason=reason,
        )

    def current_settings_hash(self) -> str:
        return self.hash_export(self.export_sanitized())

    @staticmethod
    def hash_export(exported: SanitizedSettingsExport) -> str:
        return sha256_hex(json_dumps(exported.model_dump(mode="json")))

    def _load_override_map(self) -> dict[str, str]:
        rows = self.database.fetch_all("SELECT key, value FROM settings ORDER BY key")
        return {row["key"]: row["value"] for row in rows}

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_int_list(value: Any) -> list[int]:
        if isinstance(value, list):
            return [int(item) for item in value]
        return [int(item) for item in split_csv(str(value))]
