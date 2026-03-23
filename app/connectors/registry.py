from __future__ import annotations

from app.config.settings import AppSettings
from app.connectors.base import BaseConnector
from app.connectors.filesystem import FilesystemConnector
from app.connectors.http import HttpConnector
from app.connectors.outlook import OutlookConnector
from app.connectors.system import SystemConnector
from app.connectors.task import TaskConnector
from app.core.database import Database
from app.core.errors import ConnectorError
from app.core.utils import json_dumps, utcnow_iso
from app.services.settings_service import SettingsService


class ConnectorRegistry:
    def __init__(self, base_settings: AppSettings, database: Database, settings_service: SettingsService):
        self.database = database
        self._connectors: dict[str, BaseConnector] = {
            "filesystem": FilesystemConnector(base_settings, settings_service),
            "http": HttpConnector(settings_service),
            "task": TaskConnector(database),
            "system": SystemConnector(base_settings, settings_service),
            "outlook": OutlookConnector(),
        }

    def get(self, name: str) -> BaseConnector:
        connector = self._connectors.get(name)
        if connector is None:
            raise ConnectorError(f"Unknown connector: {name}")
        return connector

    def list_health(self) -> list[dict]:
        checked_at = utcnow_iso()
        health = [connector.healthcheck() for connector in self._connectors.values()]
        for item in health:
            self.database.execute(
                """
                INSERT INTO connector_health(connector_name, available, metadata_json, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(connector_name) DO UPDATE SET
                    available = excluded.available,
                    metadata_json = excluded.metadata_json,
                    checked_at = excluded.checked_at
                """,
                (
                    item["name"],
                    int(bool(item.get("available", False))),
                    json_dumps(item),
                    checked_at,
                ),
            )
        return health
