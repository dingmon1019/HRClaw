from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.utils import ensure_parent_dir, json_dumps, utcnow_iso
from app.services.settings_service import SettingsService


class AuditService:
    def __init__(self, log_path: Path, settings_service: SettingsService):
        self.log_path = log_path
        self.settings_service = settings_service
        ensure_parent_dir(self.log_path)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        settings = self.settings_service.get_effective_settings()
        if not settings.json_audit_enabled:
            return
        record = {
            "timestamp": utcnow_iso(),
            "event_type": event_type,
            "payload": payload,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json_dumps(record))
            handle.write("\n")

