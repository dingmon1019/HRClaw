from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.config.settings import AppSettings
from app.connectors.base import BaseConnector
from app.core.errors import ConnectorError
from app.policy.path_guard import PathGuard
from app.services.settings_service import SettingsService


class FilesystemConnector(BaseConnector):
    name = "filesystem"
    description = "Local filesystem connector constrained by allowlisted roots."

    def __init__(self, base_settings: AppSettings, settings_service: SettingsService):
        self.base_settings = base_settings
        self.settings_service = settings_service
        self.path_guard = PathGuard(base_settings, settings_service)

    def healthcheck(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": True,
            "description": self.description,
            "allowed_roots": self.settings_service.get_effective_settings().allowed_filesystem_roots,
        }

    def collect(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self.path_guard.resolve_for_read(payload.get("path"))
        if path.is_dir():
            entries = sorted(child.name for child in path.iterdir())
            return {"path": str(path), "kind": "directory", "entries": entries[:50], "entry_count": len(entries)}
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="ignore")
            return {
                "path": str(path),
                "kind": "file",
                "preview": content[:2000],
                "size_bytes": path.stat().st_size,
            }
        raise ConnectorError(f"Path {path} does not exist.")

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type in {"filesystem.list_directory", "filesystem.read_text"}:
            return self.collect(payload)
        if action_type == "filesystem.write_text":
            path = self.path_guard.resolve_for_write(payload.get("path"))
            content = payload.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"path": str(path), "bytes_written": len(content.encode("utf-8"))}
        if action_type == "filesystem.append_text":
            path = self.path_guard.resolve_for_write(payload.get("path"))
            content = payload.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
            return {"path": str(path), "bytes_appended": len(content.encode("utf-8"))}
        if action_type == "filesystem.delete_path":
            path = self.path_guard.resolve_for_write(payload.get("path"))
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return {"path": str(path), "deleted": True}
        if action_type == "filesystem.make_directory":
            path = self.path_guard.resolve_for_write(payload.get("path"))
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path), "created": True}
        if action_type == "filesystem.copy_path":
            source = self.path_guard.resolve_for_read(payload.get("source_path"))
            destination = self.path_guard.resolve_for_write(payload.get("destination_path"))
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            return {"source_path": str(source), "destination_path": str(destination), "copied": True}
        if action_type == "filesystem.move_path":
            source = self.path_guard.resolve_for_write(payload.get("source_path"))
            destination = self.path_guard.resolve_for_write(payload.get("destination_path"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            return {"source_path": str(source), "destination_path": str(destination), "moved": True}
        raise ConnectorError(f"Unsupported filesystem action: {action_type}")
