from __future__ import annotations

from app.core.database import Database
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import ActionHistoryRecord, ConnectorRunRecord


class HistoryService:
    def __init__(self, database: Database):
        self.database = database

    def log_action_start(
        self,
        proposal_id: str,
        run_id: str,
        connector: str,
        action_type: str,
        payload: dict,
        provider_name: str | None = None,
        manifest_hash: str | None = None,
        correlation_id: str | None = None,
        execution_bundle_hash: str | None = None,
        boundary_mode: str | None = None,
        boundary_metadata: dict | None = None,
    ) -> str:
        history_id = new_id("history")
        self.database.execute(
            """
            INSERT INTO action_history(
                id, proposal_id, run_id, connector, action_type, status,
                started_at, completed_at, input_json, output_json, error_text, provider_name, manifest_hash, correlation_id,
                execution_bundle_hash, boundary_mode, boundary_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                proposal_id,
                run_id,
                connector,
                action_type,
                "running",
                utcnow_iso(),
                None,
                json_dumps(payload),
                None,
                None,
                provider_name,
                manifest_hash,
                correlation_id,
                execution_bundle_hash,
                boundary_mode,
                json_dumps(boundary_metadata) if boundary_metadata is not None else None,
            ),
        )
        return history_id

    def log_action_end(
        self,
        history_id: str,
        status: str,
        output: dict | None = None,
        error_text: str | None = None,
    ) -> None:
        self.database.execute(
            """
            UPDATE action_history
            SET status = ?, completed_at = ?, output_json = ?, error_text = ?
            WHERE id = ?
            """,
            (status, utcnow_iso(), json_dumps(output) if output is not None else None, error_text, history_id),
        )

    def list_action_history(self, limit: int = 100) -> list[ActionHistoryRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM action_history ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [
            ActionHistoryRecord(
                id=row["id"],
                proposal_id=row["proposal_id"],
                run_id=row["run_id"],
                connector=row["connector"],
                action_type=row["action_type"],
                status=row["status"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                input=json_loads(row["input_json"], {}),
                output=json_loads(row["output_json"], None),
                error_text=row["error_text"],
                provider_name=row["provider_name"],
                manifest_hash=row["manifest_hash"],
                correlation_id=row["correlation_id"],
                execution_bundle_hash=row["execution_bundle_hash"],
                boundary_mode=row["boundary_mode"],
                boundary_metadata=json_loads(row["boundary_metadata_json"], None),
            )
            for row in rows
        ]

    def log_connector_run(
        self,
        run_id: str,
        connector: str,
        operation: str,
        status: str,
        payload: dict,
        agent_id: str | None = None,
        agent_role: str | None = None,
        correlation_id: str | None = None,
        output: dict | None = None,
        error_text: str | None = None,
    ) -> str:
        run_record_id = new_id("connector")
        self.database.execute(
            """
            INSERT INTO connector_runs(
                id, run_id, connector, operation, status, agent_id, agent_role, correlation_id,
                input_json, output_json, error_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_record_id,
                run_id,
                connector,
                operation,
                status,
                agent_id,
                agent_role,
                correlation_id,
                json_dumps(payload),
                json_dumps(output) if output is not None else None,
                error_text,
                utcnow_iso(),
            ),
        )
        return run_record_id

    def list_connector_runs(self, limit: int = 100) -> list[ConnectorRunRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM connector_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            ConnectorRunRecord(
                id=row["id"],
                run_id=row["run_id"],
                connector=row["connector"],
                operation=row["operation"],
                status=row["status"],
                agent_id=row["agent_id"],
                agent_role=row["agent_role"],
                correlation_id=row["correlation_id"],
                input=json_loads(row["input_json"], {}),
                output=json_loads(row["output_json"], None),
                error_text=row["error_text"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
