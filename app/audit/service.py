from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.database import Database
from app.core.utils import ensure_parent_dir, json_dumps, new_id, sha256_hex, utcnow_iso
from app.services.settings_service import SettingsService


class AuditService:
    def __init__(
        self,
        database: Database,
        log_path: Path,
        settings_service: SettingsService,
        data_governance_service=None,
    ):
        self.database = database
        self.log_path = log_path
        self.settings_service = settings_service
        self.data_governance_service = data_governance_service
        ensure_parent_dir(self.log_path)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        settings = self.settings_service.get_effective_settings()
        timestamp = utcnow_iso()
        safe_payload = (
            self.data_governance_service.sanitize_for_audit(payload, object_type="audit_payload")
            if self.data_governance_service is not None
            else payload
        )
        payload_json = json_dumps(safe_payload)
        prev_row = self.database.fetch_one(
            "SELECT entry_hash FROM audit_entries ORDER BY rowid DESC LIMIT 1"
        )
        prev_hash = prev_row["entry_hash"] if prev_row else ""
        entry_hash = sha256_hex(f"{prev_hash}|{timestamp}|{event_type}|{payload_json}")
        self.database.execute(
            """
            INSERT INTO audit_entries(id, event_type, payload_json, prev_hash, entry_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("audit"), event_type, payload_json, prev_hash or None, entry_hash, timestamp),
        )
        record = {
            "timestamp": timestamp,
            "event_type": event_type,
            "payload": safe_payload,
            "prev_hash": prev_hash or None,
            "entry_hash": entry_hash,
        }
        if settings.json_audit_enabled:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json_dumps(record))
                handle.write("\n")

    def verify_integrity(self) -> dict[str, Any]:
        rows = self.database.fetch_all(
            "SELECT rowid, event_type, payload_json, prev_hash, entry_hash, created_at "
            "FROM audit_entries ORDER BY rowid ASC"
        )
        previous_hash = ""
        for index, row in enumerate(rows, start=1):
            expected = sha256_hex(
                f"{previous_hash}|{row['created_at']}|{row['event_type']}|{row['payload_json']}"
            )
            if row["entry_hash"] != expected or (row["prev_hash"] or "") != previous_hash:
                return {"ok": False, "entry_count": len(rows), "broken_at": index}
            previous_hash = row["entry_hash"]
        return {"ok": True, "entry_count": len(rows), "broken_at": None}
