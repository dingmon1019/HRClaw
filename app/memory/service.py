from __future__ import annotations

from app.core.database import Database
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import DataClassification, SummaryRecord


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
        data_classification: DataClassification,
        lineage: dict,
        outbound_summary_text: str | None = None,
    ) -> SummaryRecord:
        summary_id = new_id("summary")
        created_at = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO summaries(
                id,
                run_id,
                objective,
                collected_json,
                summary_text,
                provider_name,
                data_classification,
                lineage_json,
                outbound_summary_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary_id,
                run_id,
                objective,
                json_dumps(collected),
                summary_text,
                provider_name,
                data_classification.value,
                json_dumps(lineage),
                outbound_summary_text,
                created_at,
            ),
        )
        return SummaryRecord(
            id=summary_id,
            run_id=run_id,
            objective=objective,
            collected=collected,
            summary_text=summary_text,
            provider_name=provider_name,
            data_classification=data_classification,
            lineage=lineage,
            outbound_summary_text=outbound_summary_text,
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
                data_classification=DataClassification(row["data_classification"] or DataClassification.LOCAL_ONLY.value),
                lineage=json_loads(row["lineage_json"], {}),
                outbound_summary_text=row["outbound_summary_text"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def get_by_run_id(self, run_id: str) -> SummaryRecord | None:
        row = self.database.fetch_one(
            "SELECT * FROM summaries WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        if row is None:
            return None
        return SummaryRecord(
            id=row["id"],
            run_id=row["run_id"],
            objective=row["objective"],
            collected=json_loads(row["collected_json"], {}),
            summary_text=row["summary_text"],
            provider_name=row["provider_name"],
            data_classification=DataClassification(row["data_classification"] or DataClassification.LOCAL_ONLY.value),
            lineage=json_loads(row["lineage_json"], {}),
            outbound_summary_text=row["outbound_summary_text"],
            created_at=row["created_at"],
        )
