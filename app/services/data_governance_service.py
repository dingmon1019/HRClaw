from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.schemas.actions import DataClassification
from app.core.utils import sha256_hex
from app.security.protected_storage import ProtectedStorageService


class DataGovernanceService:
    SENSITIVE_TEXT_FIELDS = {"content", "body", "details"}
    PREVIEW_ONLY_FIELDS = {"preview", "body_preview", "before_preview", "after_preview", "diff_preview"}
    PREVIEW_LIMIT = 512

    def __init__(self, protected_storage: ProtectedStorageService):
        self.protected_storage = protected_storage

    def protect_action_payload(
        self,
        payload: dict[str, Any],
        *,
        classification: DataClassification,
        purpose: str,
    ) -> dict[str, Any]:
        protected = deepcopy(payload)
        for key in list(protected):
            value = protected.get(key)
            if key in self.SENSITIVE_TEXT_FIELDS and isinstance(value, str) and value:
                blob = self.protected_storage.store_text_blob(
                    value,
                    classification=classification.value,
                    purpose=f"{purpose}:{key}",
                )
                protected.pop(key, None)
                protected[f"{key}_digest"] = blob["digest"]
                protected[f"{key}_preview"] = (
                    f"[protected:{classification.value};len={len(value)}]"
                )
                protected[f"{key}_blob_id"] = blob["blob_id"]
                protected[f"{key}_storage"] = blob["storage_mode"]
        return protected

    def materialize_action_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        materialized = deepcopy(payload)
        for key in self.SENSITIVE_TEXT_FIELDS:
            blob_id = materialized.get(f"{key}_blob_id")
            digest = materialized.get(f"{key}_digest")
            if blob_id:
                materialized[key] = self.protected_storage.load_text_blob(blob_id, expected_digest=digest)
        return materialized

    def sanitize_for_history(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, child in value.items():
                if key.endswith("_blob_id"):
                    continue
                if key in self.SENSITIVE_TEXT_FIELDS and isinstance(child, str):
                    redacted[key] = {
                        "redacted": True,
                        "digest": sha256_hex(child),
                        "length": len(child),
                    }
                    continue
                if key in self.PREVIEW_ONLY_FIELDS and isinstance(child, str):
                    redacted[key] = child[: self.PREVIEW_LIMIT]
                    continue
                redacted[key] = self.sanitize_for_history(child)
            return redacted
        if isinstance(value, list):
            return [self.sanitize_for_history(item) for item in value[:100]]
        if isinstance(value, str):
            return value[: self.PREVIEW_LIMIT]
        return value
