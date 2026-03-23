from __future__ import annotations

from pathlib import Path

from app.config.settings import AppSettings
from app.core.errors import ConnectorError
from app.services.settings_service import SettingsService


class PathGuard:
    PROTECTED_NAMES = {
        "app",
        "ui",
        "tests",
        "docs",
        "scripts",
        ".git",
        "data",
    }
    PROTECTED_FILES = {
        "main.py",
        "README.md",
        "requirements.txt",
        "LICENSE",
        ".env",
        ".env.example",
    }
    PROTECTED_SUFFIXES = {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".jsonl",
        ".log",
        ".toml",
        ".ini",
        ".yaml",
        ".yml",
        ".env",
    }

    def __init__(self, base_settings: AppSettings, settings_service: SettingsService):
        self.base_settings = base_settings
        self.settings_service = settings_service

    def resolve_for_read(self, raw_path: str | None) -> Path:
        candidate = self.resolve_for_probe(raw_path)
        if not candidate.exists():
            raise ConnectorError(f"Path {candidate} does not exist.")
        return candidate

    def resolve_for_write(self, raw_path: str | None) -> Path:
        candidate = self.resolve_for_probe(raw_path)
        self._assert_not_protected_write_path(candidate)
        return candidate

    def resolve_for_probe(self, raw_path: str | None) -> Path:
        candidate = self._resolve_candidate(raw_path)
        self._assert_no_symlink_chain(candidate)
        normalized = candidate.resolve(strict=False)
        self._assert_allowed_root(normalized)
        return normalized

    def check_payload(self, payload: dict, write: bool) -> str | None:
        keys = ("path", "source_path", "destination_path")
        for key in keys:
            raw_value = payload.get(key)
            if not raw_value:
                continue
            try:
                if write and key != "source_path":
                    self.resolve_for_write(raw_value)
                else:
                    self.resolve_for_read(raw_value)
            except ConnectorError as exc:
                return str(exc)
        return None

    def allowed_root_paths(self) -> list[Path]:
        settings = self.settings_service.get_effective_settings()
        roots: list[Path] = []
        for raw_root in settings.allowed_filesystem_roots:
            root = Path(raw_root)
            if not root.is_absolute():
                root = (self.base_settings.resolved_runtime_state_root / root).resolve()
            else:
                root = root.resolve()
            roots.append(root)
        return roots

    def _resolve_candidate(self, raw_path: str | None) -> Path:
        if not raw_path:
            raise ConnectorError("Filesystem actions require a path.")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self.base_settings.resolved_workspace_root / candidate).absolute()
        else:
            candidate = candidate.absolute()
        return candidate

    def _assert_allowed_root(self, candidate: Path) -> None:
        if not any(self._is_relative_to(candidate, root) for root in self.allowed_root_paths()):
            raise ConnectorError(f"Path {candidate} is outside the configured workspace allowlist.")

    def _assert_not_protected_write_path(self, candidate: Path) -> None:
        protected_paths = [self.base_settings.project_root / name for name in self.PROTECTED_NAMES]
        protected_paths.extend(self.base_settings.project_root / name for name in self.PROTECTED_FILES)
        protected_paths.append(self.base_settings.resolved_database_path)
        protected_paths.append(self.base_settings.resolved_audit_log_path)
        protected_paths.append(self.base_settings.resolved_session_secret_path)
        protected_paths.append(self.base_settings.resolved_data_dir)
        protected_paths.append(self.base_settings.resolved_logs_dir)
        protected_paths.append(self.base_settings.resolved_secrets_dir)
        for protected in protected_paths:
            if candidate == protected or self._is_relative_to(candidate, protected):
                raise ConnectorError(f"Writes to protected path {protected} are not allowed.")
        if candidate.name.lower().startswith(".env"):
            raise ConnectorError("Writes to environment or secret files are not allowed.")
        if candidate.suffix.lower() in self.PROTECTED_SUFFIXES:
            raise ConnectorError(f"Writes to protected file type {candidate.suffix} are not allowed.")

    def _assert_no_symlink_chain(self, candidate: Path) -> None:
        current = candidate if candidate.exists() else candidate.parent
        while True:
            if current.exists() and current.is_symlink():
                raise ConnectorError(f"Symlink traversal is not allowed: {current}")
            if current == current.parent:
                break
            current = current.parent

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
