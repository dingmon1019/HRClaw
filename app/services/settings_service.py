from __future__ import annotations

from typing import Any

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.utils import split_csv, utcnow_iso
from app.schemas.actions import RuntimeMode
from app.schemas.settings import EffectiveSettings, SettingsUpdate


class SettingsService:
    def __init__(self, base_settings: AppSettings, database: Database):
        self.base_settings = base_settings
        self.database = database

    def get_effective_settings(self) -> EffectiveSettings:
        overrides = self._load_override_map()
        runtime_mode = RuntimeMode(overrides.get("runtime_mode", self.base_settings.runtime_mode))
        return EffectiveSettings(
            app_name=self.base_settings.app_name,
            runtime_mode=runtime_mode,
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
            json_audit_enabled=self._parse_bool(
                overrides.get("json_audit_enabled", self.base_settings.json_audit_enabled)
            ),
            allowed_filesystem_roots=split_csv(
                overrides.get(
                    "allowed_filesystem_roots",
                    self.base_settings.allowed_filesystem_roots,
                )
            ),
            allowed_http_hosts=split_csv(
                overrides.get("allowed_http_hosts", self.base_settings.allowed_http_hosts)
            ),
            powershell_allowlist=split_csv(
                overrides.get("powershell_allowlist", self.base_settings.powershell_allowlist)
            ),
        )

    def save(self, update: SettingsUpdate) -> EffectiveSettings:
        payload: dict[str, Any] = update.model_dump()
        for key, value in payload.items():
            if value is None:
                continue
            self.database.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(value), utcnow_iso()),
            )
        return self.get_effective_settings()

    def list_raw_settings(self) -> dict[str, str]:
        return self._load_override_map()

    def _load_override_map(self) -> dict[str, str]:
        rows = self.database.fetch_all("SELECT key, value FROM settings ORDER BY key")
        return {row["key"]: row["value"] for row in rows}

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

