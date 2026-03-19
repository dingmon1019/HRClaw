from __future__ import annotations

from typing import Any

from app.connectors.base import BaseConnector
from app.core.database import Database
from app.core.errors import ConnectorError
from app.core.utils import new_id, utcnow_iso


class TaskConnector(BaseConnector):
    name = "task"
    description = "Local task connector backed by SQLite."

    def __init__(self, database: Database):
        self.database = database

    def healthcheck(self) -> dict[str, Any]:
        count = self.database.fetch_one("SELECT COUNT(*) AS count FROM tasks")
        return {
            "name": self.name,
            "available": True,
            "description": self.description,
            "open_tasks": count["count"] if count else 0,
        }

    def collect(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = int(payload.get("limit", 10))
        rows = self.database.fetch_all(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return {
            "tasks": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "details": row["details"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type == "task.list":
            return self.collect(payload)
        if action_type == "task.create":
            title = (payload.get("title") or "").strip()
            details = payload.get("details") or ""
            if not title:
                raise ConnectorError("Task creation requires a title.")
            task_id = new_id("task")
            now = utcnow_iso()
            self.database.execute(
                """
                INSERT INTO tasks(id, title, details, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, title, details, "open", now, now),
            )
            return {"task_id": task_id, "title": title, "status": "open"}
        if action_type == "task.complete":
            task_id = payload.get("task_id")
            if not task_id:
                raise ConnectorError("Task completion requires a task_id.")
            self.database.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("completed", utcnow_iso(), task_id),
            )
            return {"task_id": task_id, "status": "completed"}
        raise ConnectorError(f"Unsupported task action: {action_type}")

