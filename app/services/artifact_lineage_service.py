from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import ArtifactEventRecord


class ArtifactLineageService:
    TERMINAL_NODE_STATES = {"completed", "executed", "failed", "blocked", "cancelled"}

    def __init__(self, database: Database, base_settings: AppSettings):
        self.database = database
        self.base_settings = base_settings

    def record_work_area_scope(
        self,
        *,
        run_id: str,
        proposal_id: str | None,
        agent_role: str,
        context_namespace: str,
        artifact_path: str,
        details: dict[str, Any],
    ) -> ArtifactEventRecord:
        return self._insert_event(
            run_id=run_id,
            proposal_id=proposal_id,
            agent_role=agent_role,
            context_namespace=context_namespace,
            event_type="work-area-assigned",
            artifact_path=artifact_path,
            source_path=None,
            destination_path=None,
            status="active",
            details=details,
        )

    def record_execution_artifacts(
        self,
        *,
        run_id: str,
        proposal_id: str,
        agent_role: str,
        context_namespace: str | None,
        action_type: str,
        payload: dict[str, Any],
        result: dict[str, Any],
        scratch_root: str | None,
        promotion_root: str | None,
        shared_workspace_root: str | None,
    ) -> list[ArtifactEventRecord]:
        events: list[ArtifactEventRecord] = []
        candidate_paths = {
            "path": result.get("path") or payload.get("path"),
            "source_path": result.get("source_path") or payload.get("source_path"),
            "destination_path": result.get("destination_path") or payload.get("destination_path"),
        }
        source_path = self._coerce_path(candidate_paths["source_path"])
        destination_path = self._coerce_path(candidate_paths["destination_path"])
        artifact_path = self._coerce_path(candidate_paths["path"])
        scratch = self._coerce_path(scratch_root)
        promotion = self._coerce_path(promotion_root)
        shared = self._coerce_path(shared_workspace_root)

        if action_type in {"filesystem.copy_path", "filesystem.move_path"} and source_path and destination_path:
            details = {
                "action_type": action_type,
                "source_scope": self._path_scope(source_path, scratch=scratch, promotion=promotion, shared=shared),
                "destination_scope": self._path_scope(destination_path, scratch=scratch, promotion=promotion, shared=shared),
            }
            event_type = "promotion" if details["source_scope"] in {"scratch", "promotion"} and details["destination_scope"] == "shared" else "filesystem-transfer"
            events.append(
                self._insert_event(
                    run_id=run_id,
                    proposal_id=proposal_id,
                    agent_role=agent_role,
                    context_namespace=context_namespace,
                    event_type=event_type,
                    artifact_path=str(destination_path),
                    source_path=str(source_path),
                    destination_path=str(destination_path),
                    status="recorded",
                    details=details,
                )
            )
        elif artifact_path and action_type.startswith("filesystem."):
            scope = self._path_scope(artifact_path, scratch=scratch, promotion=promotion, shared=shared)
            if scope in {"scratch", "promotion", "shared"}:
                events.append(
                    self._insert_event(
                        run_id=run_id,
                        proposal_id=proposal_id,
                        agent_role=agent_role,
                        context_namespace=context_namespace,
                        event_type=f"{scope}-write",
                        artifact_path=str(artifact_path),
                        source_path=None,
                        destination_path=str(artifact_path),
                        status="recorded",
                        details={"action_type": action_type, "scope": scope},
                    )
                )
        return events

    def list_events(self, run_id: str, limit: int = 200) -> list[ArtifactEventRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM artifact_events WHERE run_id = ? ORDER BY created_at ASC LIMIT ?",
            (run_id, limit),
        )
        return [self._row_to_record(row) for row in rows]

    def cleanup_stale_work_areas(
        self,
        *,
        dry_run: bool = True,
        retention_days: int | None = None,
    ) -> dict[str, Any]:
        root = self.base_settings.resolved_runtime_state_root / "agent_workspaces"
        if not root.exists():
            return {"removed": [], "skipped": [], "dry_run": dry_run}
        keep_days = retention_days or self.base_settings.history_retention_days
        cutoff = datetime.now(UTC) - timedelta(days=keep_days)
        removed: list[str] = []
        skipped: list[dict[str, str]] = []
        for run_root in sorted(path for path in root.iterdir() if path.is_dir()):
            run_id = run_root.name
            mtime = datetime.fromtimestamp(run_root.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                skipped.append({"run_id": run_id, "reason": "retention-window"})
                continue
            active = self.database.fetch_one(
                """
                SELECT id FROM task_nodes
                WHERE run_id = ?
                  AND status NOT IN ('completed', 'executed', 'failed', 'blocked', 'cancelled')
                LIMIT 1
                """,
                (run_id,),
            )
            if active is not None:
                skipped.append({"run_id": run_id, "reason": "active-task-nodes"})
                continue
            if dry_run:
                removed.append(str(run_root))
                continue
            shutil.rmtree(run_root, ignore_errors=True)
            removed.append(str(run_root))
            self._insert_event(
                run_id=run_id,
                proposal_id=None,
                agent_role="runtime",
                context_namespace=None,
                event_type="work-area-cleanup",
                artifact_path=str(run_root),
                source_path=None,
                destination_path=None,
                status="removed",
                details={"retention_days": keep_days},
            )
        return {"removed": removed, "skipped": skipped, "dry_run": dry_run}

    def _insert_event(
        self,
        *,
        run_id: str,
        proposal_id: str | None,
        agent_role: str,
        context_namespace: str | None,
        event_type: str,
        artifact_path: str | None,
        source_path: str | None,
        destination_path: str | None,
        status: str,
        details: dict[str, Any],
    ) -> ArtifactEventRecord:
        record = ArtifactEventRecord(
            id=new_id("artifact"),
            run_id=run_id,
            proposal_id=proposal_id,
            agent_role=agent_role,
            context_namespace=context_namespace,
            event_type=event_type,
            artifact_path=artifact_path,
            source_path=source_path,
            destination_path=destination_path,
            status=status,
            details=details,
            created_at=utcnow_iso(),
        )
        self.database.execute(
            """
            INSERT INTO artifact_events(
                id, run_id, proposal_id, agent_role, context_namespace, event_type,
                artifact_path, source_path, destination_path, status, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.run_id,
                record.proposal_id,
                record.agent_role,
                record.context_namespace,
                record.event_type,
                record.artifact_path,
                record.source_path,
                record.destination_path,
                record.status,
                json_dumps(record.details),
                record.created_at,
            ),
        )
        return record

    @staticmethod
    def _row_to_record(row) -> ArtifactEventRecord:
        return ArtifactEventRecord(
            id=row["id"],
            run_id=row["run_id"],
            proposal_id=row["proposal_id"],
            agent_role=row["agent_role"],
            context_namespace=row["context_namespace"],
            event_type=row["event_type"],
            artifact_path=row["artifact_path"],
            source_path=row["source_path"],
            destination_path=row["destination_path"],
            status=row["status"],
            details=json_loads(row["details_json"], {}),
            created_at=row["created_at"],
        )

    @staticmethod
    def _coerce_path(value: str | None) -> Path | None:
        return Path(value).resolve(strict=False) if value else None

    @staticmethod
    def _is_relative_to(path: Path, root: Path | None) -> bool:
        if root is None:
            return False
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _path_scope(self, path: Path, *, scratch: Path | None, promotion: Path | None, shared: Path | None) -> str:
        if self._is_relative_to(path, scratch):
            return "scratch"
        if self._is_relative_to(path, promotion):
            return "promotion"
        if self._is_relative_to(path, shared):
            return "shared"
        return "external"
