from __future__ import annotations

import os
from typing import Any

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.utils import json_dumps, json_loads, new_id, sha256_hex, split_csv, utcnow_iso
from app.schemas.actions import RuntimeMode
from app.schemas.providers import ProviderConfigRecord, ProviderConfigUpdate
from app.schemas.settings import EffectiveSettings, SanitizedSettingsExport, SettingsUpdate


class SettingsService:
    def __init__(self, base_settings: AppSettings, database: Database):
        self.base_settings = base_settings
        self.database = database

    def get_effective_settings(self) -> EffectiveSettings:
        overrides = self._load_override_map()
        provider_configs = self._load_provider_configs(overrides)
        runtime_mode = RuntimeMode(overrides.get("runtime_mode", self.base_settings.runtime_mode))
        configured_secret_envs = [
            env_name
            for env_name in {
                overrides.get("api_key_env", self.base_settings.api_key_env),
                self.base_settings.anthropic_api_key_env,
                self.base_settings.gemini_api_key_env,
                *[config.api_key_env for config in provider_configs if config.api_key_env],
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
            provider_configs=provider_configs,
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
            cli_auth_mode="interactive-short-lived",
            local_protection_mode=self._normalize_protection_mode(
                overrides.get("local_protection_mode", self.base_settings.local_protection_mode)
            ),
            allow_insecure_local_storage=self._parse_bool(
                overrides.get(
                    "allow_insecure_local_storage",
                    self.base_settings.allow_insecure_local_storage,
                )
            ),
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
        *,
        record_version: bool = True,
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
        if record_version:
            self._record_settings_version(actor=actor, reason=reason, created_at=updated_at)
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
            provider_configs=[
                config.model_copy(update={"updated_at": None})
                for config in settings.provider_configs
            ],
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
            allow_insecure_local_storage=settings.allow_insecure_local_storage,
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
        effective = self.save(
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
                allow_insecure_local_storage=exported.allow_insecure_local_storage,
                history_retention_days=exported.history_retention_days,
                cli_token_ttl_seconds=exported.cli_token_ttl_seconds,
                worker_lease_seconds=exported.worker_lease_seconds,
                worker_max_attempts=exported.worker_max_attempts,
            ),
            actor=actor,
            reason=reason,
            record_version=False,
        )
        self.database.execute("DELETE FROM provider_configs")
        for record in exported.provider_configs:
            self._save_provider_config_record(
                ProviderConfigUpdate(**record.model_dump(mode="json")),
                actor=actor,
                reason=reason,
                record_version=False,
            )
        self._record_settings_version(actor=actor, reason=reason)
        return effective

    def reset_to_safe_defaults(
        self,
        actor: str = "system",
        reason: str | None = None,
    ) -> EffectiveSettings:
        effective = self.save(
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
                allow_insecure_local_storage=self.base_settings.allow_insecure_local_storage,
                history_retention_days=self.base_settings.history_retention_days,
                cli_token_ttl_seconds=self.base_settings.cli_token_ttl_seconds,
                worker_lease_seconds=self.base_settings.worker_lease_seconds,
                worker_max_attempts=self.base_settings.worker_max_attempts,
            ),
            actor=actor,
            reason=reason,
            record_version=False,
        )
        self.database.execute("DELETE FROM provider_configs")
        self._record_settings_version(actor=actor, reason=reason)
        return effective

    def current_settings_hash(self) -> str:
        return self.hash_export(self.export_sanitized())

    @staticmethod
    def hash_export(exported: SanitizedSettingsExport) -> str:
        return sha256_hex(json_dumps(exported.model_dump(mode="json")))

    def list_provider_configs(self) -> list[ProviderConfigRecord]:
        return self.get_effective_settings().provider_configs

    def get_provider_config(self, provider_name: str) -> ProviderConfigRecord:
        for record in self.list_provider_configs():
            if record.provider_name == provider_name:
                return record
        raise KeyError(f"Unknown provider config: {provider_name}")

    def save_provider_config(
        self,
        update: ProviderConfigUpdate,
        *,
        actor: str = "system",
        reason: str | None = None,
    ) -> ProviderConfigRecord:
        record = self._save_provider_config_record(update, actor=actor, reason=reason, record_version=False)
        self._record_settings_version(actor=actor, reason=reason)
        return record

    def reset_provider_config(
        self,
        provider_name: str,
        *,
        actor: str = "system",
        reason: str | None = None,
    ) -> ProviderConfigRecord:
        self.database.execute("DELETE FROM provider_configs WHERE provider_name = ?", (provider_name,))
        self._record_settings_version(actor=actor, reason=reason)
        return self.get_provider_config(provider_name)

    def _load_override_map(self) -> dict[str, str]:
        rows = self.database.fetch_all("SELECT key, value FROM settings ORDER BY key")
        return {row["key"]: row["value"] for row in rows}

    def _load_provider_configs(self, overrides: dict[str, str]) -> list[ProviderConfigRecord]:
        rows = self.database.fetch_all("SELECT * FROM provider_configs ORDER BY provider_name")
        row_map = {row["provider_name"]: row for row in rows}
        provider_names = ["mock", "openai", "openai-compatible", "generic-http", "anthropic", "gemini"]
        return [self._provider_config_from_row(name, row_map.get(name), overrides) for name in provider_names]

    def _provider_config_from_row(
        self,
        provider_name: str,
        row,
        overrides: dict[str, str],
    ) -> ProviderConfigRecord:
        defaults = self._default_provider_config(provider_name, overrides)
        if row is None:
            return defaults
        return ProviderConfigRecord(
            provider_name=provider_name,
            enabled=bool(row["enabled"]),
            base_url=row["base_url"] or defaults.base_url,
            generic_http_endpoint=row["generic_http_endpoint"] or defaults.generic_http_endpoint,
            api_key_env=row["api_key_env"] or defaults.api_key_env,
            default_model=row["default_model"] or defaults.default_model,
            allowed_hosts=json_loads(row["allowed_hosts_json"], defaults.allowed_hosts),
            auth_source=row["auth_source"] or defaults.auth_source,
            updated_at=row["updated_at"],
        )

    def _default_provider_config(self, provider_name: str, overrides: dict[str, str]) -> ProviderConfigRecord:
        shared_hosts = split_csv(
            overrides.get("provider_allowed_hosts", self.base_settings.provider_allowed_hosts)
        )
        base_url = overrides.get("base_url", self.base_settings.base_url)
        generic_http_endpoint = overrides.get(
            "generic_http_endpoint",
            self.base_settings.generic_http_endpoint,
        )
        model = overrides.get("model", self.base_settings.model)
        api_key_env = overrides.get("api_key_env", self.base_settings.api_key_env)
        provider_defaults: dict[str, dict[str, Any]] = {
            "mock": {
                "enabled": True,
                "default_model": "mock-model",
                "allowed_hosts": [],
                "api_key_env": None,
            },
            "openai": {
                "enabled": True,
                "base_url": base_url,
                "api_key_env": api_key_env,
                "default_model": model,
                "allowed_hosts": shared_hosts,
            },
            "openai-compatible": {
                "enabled": True,
                "base_url": base_url,
                "api_key_env": api_key_env,
                "default_model": model,
                "allowed_hosts": shared_hosts,
            },
            "generic-http": {
                "enabled": True,
                "base_url": base_url,
                "generic_http_endpoint": generic_http_endpoint,
                "api_key_env": api_key_env,
                "default_model": model,
                "allowed_hosts": shared_hosts,
            },
            "anthropic": {
                "enabled": True,
                "api_key_env": self.base_settings.anthropic_api_key_env,
                "default_model": model,
                "allowed_hosts": shared_hosts,
            },
            "gemini": {
                "enabled": True,
                "api_key_env": self.base_settings.gemini_api_key_env,
                "default_model": model,
                "allowed_hosts": shared_hosts,
            },
        }
        merged = provider_defaults.get(provider_name, {"enabled": True, "allowed_hosts": shared_hosts})
        return ProviderConfigRecord(
            provider_name=provider_name,
            enabled=bool(merged.get("enabled", True)),
            base_url=merged.get("base_url"),
            generic_http_endpoint=merged.get("generic_http_endpoint"),
            api_key_env=merged.get("api_key_env"),
            default_model=merged.get("default_model"),
            allowed_hosts=list(merged.get("allowed_hosts", shared_hosts)),
            auth_source=str(merged.get("auth_source", "env")),
            updated_at=None,
        )

    def _save_provider_config_record(
        self,
        update: ProviderConfigUpdate,
        *,
        actor: str,
        reason: str | None,
        record_version: bool,
    ) -> ProviderConfigRecord:
        updated_at = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO provider_configs(
                provider_name, enabled, base_url, generic_http_endpoint, api_key_env, default_model,
                allowed_hosts_json, auth_source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_name) DO UPDATE SET
                enabled = excluded.enabled,
                base_url = excluded.base_url,
                generic_http_endpoint = excluded.generic_http_endpoint,
                api_key_env = excluded.api_key_env,
                default_model = excluded.default_model,
                allowed_hosts_json = excluded.allowed_hosts_json,
                auth_source = excluded.auth_source,
                updated_at = excluded.updated_at
            """,
            (
                update.provider_name,
                int(update.enabled),
                update.base_url,
                update.generic_http_endpoint,
                update.api_key_env,
                update.default_model,
                json_dumps(update.allowed_hosts),
                update.auth_source,
                updated_at,
            ),
        )
        if record_version:
            self._record_settings_version(actor=actor, reason=reason, created_at=updated_at)
        return self.get_provider_config(update.provider_name)

    def _record_settings_version(
        self,
        *,
        actor: str,
        reason: str | None,
        created_at: str | None = None,
    ) -> None:
        export = self.export_sanitized()
        created = created_at or utcnow_iso()
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
                created,
            ),
        )

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

    @staticmethod
    def _normalize_protection_mode(value: Any) -> str:
        candidate = str(value or "").strip().lower()
        if candidate in {"plain-local", "unprotected-local"}:
            return "unprotected-local"
        return "dpapi"
