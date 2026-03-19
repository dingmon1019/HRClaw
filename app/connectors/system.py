from __future__ import annotations

import subprocess
from typing import Any

from app.connectors.base import BaseConnector
from app.core.errors import ConnectorError
from app.services.settings_service import SettingsService


class SystemConnector(BaseConnector):
    name = "system"
    description = "Windows PowerShell connector constrained by an allowlist."

    def __init__(self, settings_service: SettingsService):
        self.settings_service = settings_service

    def healthcheck(self) -> dict[str, Any]:
        settings = self.settings_service.get_effective_settings()
        return {
            "name": self.name,
            "available": True,
            "description": self.description,
            "allowlist_size": len(settings.powershell_allowlist),
        }

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type != "system.powershell":
            raise ConnectorError(f"Unsupported system action: {action_type}")
        command = (payload.get("command") or "").strip()
        if not command:
            raise ConnectorError("PowerShell execution requires a command.")
        first_token = command.split()[0]
        allowlist = self.settings_service.get_effective_settings().powershell_allowlist
        if allowlist and first_token not in allowlist:
            raise ConnectorError(f"Command {first_token} is not in the configured allowlist.")
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }

