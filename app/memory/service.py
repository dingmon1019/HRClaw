from __future__ import annotations

from app.core.database import Database
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import SummaryRecord


class SummaryService:
    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        run_id: str,
        objective: str,
        collected: dict,
        summary_text: str,
        provider_name: str,
    ) -> SummaryRecord:
        summary_id = new_id("summary")
        created_at = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO summaries(id, run_id, objective, collected_json, summary_text, provider_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (summary_id, run_id, objective, json_dumps(collected), summary_text, provider_name, created_at),
        )
        return SummaryRecord(
            id=summary_id,
            run_id=run_id,
            objective=objective,
            collected=collected,
            summary_text=summary_text,
            provider_name=provider_name,
            created_at=created_at,
        )

    def list_recent(self, limit: int = 20) -> list[SummaryRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM summaries ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            SummaryRecord(
                id=row["id"],
                run_id=row["run_id"],
                objective=row["objective"],
                collected=json_loads(row["collected_json"], {}),
                summary_text=row["summary_text"],
                provider_name=row["provider_name"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

