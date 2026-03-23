from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.connectors.base import BaseConnector
from app.core.errors import ConnectorError
from app.policy.path_guard import PathGuard
from app.services.settings_service import SettingsService


SAFE_SYSTEM_ACTIONS = {
    "system.list_directory",
    "system.read_text_file",
    "system.test_path",
    "system.get_time",
}


class SystemConnector(BaseConnector):
    name = "system"
    description = "Schema-driven system connector with bounded read-only actions."

    def __init__(self, base_settings, settings_service: SettingsService):
        self.base_settings = base_settings
        self.settings_service = settings_service
        self.path_guard = PathGuard(base_settings, settings_service)

    def healthcheck(self) -> dict[str, Any]:
        settings = self.settings_service.get_effective_settings()
        return {
            "name": self.name,
            "available": settings.enable_system_connector,
            "description": self.description,
            "supported_actions": sorted(SAFE_SYSTEM_ACTIONS),
        }

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings_service.get_effective_settings().enable_system_connector:
            raise ConnectorError("The bounded system connector is disabled.")
        if action_type not in SAFE_SYSTEM_ACTIONS:
            raise ConnectorError(f"Unsupported system action: {action_type}")
        if action_type == "system.get_time":
            return {"utc_time": datetime.now(UTC).replace(microsecond=0).isoformat()}
        if action_type == "system.test_path":
            path = self.path_guard.resolve_for_probe(payload.get("path"))
            return {"path": str(path), "exists": path.exists(), "is_file": path.is_file(), "is_dir": path.is_dir()}
        if action_type == "system.list_directory":
            path = self.path_guard.resolve_for_read(payload.get("path"))
            if not path.is_dir():
                raise ConnectorError(f"Path {path} is not a directory.")
            return {"path": str(path), "entries": sorted(child.name for child in path.iterdir())[:100]}
        if action_type == "system.read_text_file":
            path = self.path_guard.resolve_for_read(payload.get("path"))
            if not path.is_file():
                raise ConnectorError(f"Path {path} is not a file.")
            content = path.read_text(encoding="utf-8", errors="ignore")
            return {"path": str(path), "preview": content[:4000], "size_bytes": len(content.encode("utf-8"))}
        raise ConnectorError(f"Unsupported system action: {action_type}")
