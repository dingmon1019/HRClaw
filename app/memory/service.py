from __future__ import annotations

from app.core.errors import ProtectedStorageRefusalError
from app.core.utils import sha256_hex
from app.core.database import Database
from app.core.utils import json_dumps, json_loads, new_id, utcnow_iso
from app.schemas.actions import DataClassification, SummaryRecord
from app.security.protected_storage import ProtectedStorageService


class SummaryService:
    PREVIEW_LIMIT = 512
    SENSITIVE_PREVIEW_LIMIT = 96

    def __init__(self, database: Database, protected_storage: ProtectedStorageService):
        self.database = database
        self.protected_storage = protected_storage

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
        stored_summary = self._prepare_storage(
            summary_text,
            data_classification=data_classification,
            purpose="summary-text",
        )
        stored_outbound = self._prepare_storage(
            outbound_summary_text,
            data_classification=data_classification,
            purpose="summary-outbound-text",
        )
        self.database.execute(
            """
            INSERT INTO summaries(
                id,
                run_id,
                objective,
                collected_json,
                summary_text,
                summary_text_blob_id,
                summary_text_digest,
                summary_text_storage_mode,
                summary_text_storage_class,
                summary_text_encoding,
                provider_name,
                data_classification,
                lineage_json,
                outbound_summary_text,
                outbound_summary_text_blob_id,
                outbound_summary_text_digest,
                outbound_summary_text_storage_mode,
                outbound_summary_text_storage_class,
                outbound_summary_text_encoding,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary_id,
                run_id,
                objective,
                json_dumps(collected),
                stored_summary["stored_text"],
                stored_summary["blob_id"],
                stored_summary["digest"],
                stored_summary["storage_mode"],
                stored_summary["storage_class"],
                stored_summary["encoding"],
                provider_name,
                data_classification.value,
                json_dumps(lineage),
                stored_outbound["stored_text"],
                stored_outbound["blob_id"],
                stored_outbound["digest"],
                stored_outbound["storage_mode"],
                stored_outbound["storage_class"],
                stored_outbound["encoding"],
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
            summary_storage_mode=stored_summary["storage_mode"],
            outbound_storage_mode=stored_outbound["storage_mode"],
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
                summary_storage_mode=row["summary_text_storage_mode"] or "direct",
                outbound_storage_mode=row["outbound_summary_text_storage_mode"],
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
            summary_text=self._materialize_text(row, field_name="summary_text"),
            provider_name=row["provider_name"],
            data_classification=DataClassification(row["data_classification"] or DataClassification.LOCAL_ONLY.value),
            lineage=json_loads(row["lineage_json"], {}),
            outbound_summary_text=self._materialize_text(row, field_name="outbound_summary_text"),
            summary_storage_mode=row["summary_text_storage_mode"] or "direct",
            outbound_storage_mode=row["outbound_summary_text_storage_mode"],
            created_at=row["created_at"],
        )

    def _prepare_storage(
        self,
        text: str | None,
        *,
        data_classification: DataClassification,
        purpose: str,
    ) -> dict[str, str | None]:
        if text is None:
            return {
                "stored_text": None,
                "blob_id": None,
                "digest": None,
                "storage_mode": None,
                "storage_class": None,
                "encoding": None,
            }
        if data_classification == DataClassification.EXTERNAL_OK:
            return {
                "stored_text": text,
                "blob_id": None,
                "digest": sha256_hex(text),
                "storage_mode": "direct",
                "storage_class": None,
                "encoding": "text",
            }
        storage_class = (
            "privileged-sensitive"
            if data_classification == DataClassification.RESTRICTED
            else "sensitive-local"
        )
        try:
            blob = self.protected_storage.store_text_blob(
                text,
                classification=storage_class,
                purpose=purpose,
            )
            return {
                "stored_text": self._protected_preview_marker(
                    data_classification=data_classification,
                    purpose=purpose,
                ),
                "blob_id": blob["blob_id"],
                "digest": blob["digest"],
                "storage_mode": f"protected-blob:{blob['storage_mode']}",
                "storage_class": storage_class,
                "encoding": "text",
            }
        except ProtectedStorageRefusalError:
            return {
                "stored_text": self._preview_fragment(text, sensitive=True),
                "blob_id": None,
                "digest": sha256_hex(text),
                "storage_mode": "preview-only",
                "storage_class": storage_class,
                "encoding": "text",
            }

    def _materialize_text(self, row, *, field_name: str) -> str | None:
        stored_text = row[field_name]
        blob_id = row[f"{field_name}_blob_id"]
        digest = row[f"{field_name}_digest"]
        if blob_id:
            return self.protected_storage.load_text_blob(blob_id, expected_digest=digest)
        return stored_text

    def _preview_fragment(self, text: str, *, sensitive: bool) -> str:
        limit = self.SENSITIVE_PREVIEW_LIMIT if sensitive else self.PREVIEW_LIMIT
        return text[:limit]

    @staticmethod
    def _protected_preview_marker(*, data_classification: DataClassification, purpose: str) -> str:
        label = "restricted" if data_classification == DataClassification.RESTRICTED else "local-only"
        if "outbound" in purpose:
            return f"[protected {label} outbound summary]"
        return f"[protected {label} summary]"
