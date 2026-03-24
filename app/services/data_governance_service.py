from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Iterable
from urllib.parse import urlsplit

from app.core.utils import json_dumps, sha256_hex
from app.schemas.actions import DataClassification
from app.security.protected_storage import ProtectedStorageService


class DataGovernanceService:
    NON_SENSITIVE = "non-sensitive"
    PREVIEW_ONLY = "preview-only"
    SENSITIVE_LOCAL = "sensitive-local"
    PRIVILEGED_SENSITIVE = "privileged-sensitive"
    STORAGE_FIELDS = {SENSITIVE_LOCAL, PRIVILEGED_SENSITIVE}
    PREVIEW_LIMIT = 512
    SENSITIVE_HEADER_KEYS = {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
    }
    ACTION_FIELD_REGISTRY: dict[str, dict[str, str]] = {
        "filesystem.write_text": {"path": NON_SENSITIVE, "content": PRIVILEGED_SENSITIVE},
        "filesystem.append_text": {"path": NON_SENSITIVE, "content": PRIVILEGED_SENSITIVE},
        "filesystem.read_text": {"path": NON_SENSITIVE},
        "filesystem.list_directory": {"path": NON_SENSITIVE},
        "filesystem.delete_path": {"path": NON_SENSITIVE},
        "filesystem.make_directory": {"path": NON_SENSITIVE},
        "http.get": {"url": NON_SENSITIVE, "headers": SENSITIVE_LOCAL},
        "http.head": {"url": NON_SENSITIVE, "headers": SENSITIVE_LOCAL},
        "http.post": {"url": NON_SENSITIVE, "headers": SENSITIVE_LOCAL, "body": PRIVILEGED_SENSITIVE},
        "http.put": {"url": NON_SENSITIVE, "headers": SENSITIVE_LOCAL, "body": PRIVILEGED_SENSITIVE},
        "http.patch": {"url": NON_SENSITIVE, "headers": SENSITIVE_LOCAL, "body": PRIVILEGED_SENSITIVE},
        "http.delete": {"url": NON_SENSITIVE, "headers": SENSITIVE_LOCAL, "body": PRIVILEGED_SENSITIVE},
        "task.create": {"title": PREVIEW_ONLY, "details": SENSITIVE_LOCAL},
        "task.list": {"limit": NON_SENSITIVE},
        "system.list_directory": {"path": NON_SENSITIVE},
        "system.read_text_file": {"path": NON_SENSITIVE},
        "system.test_path": {"path": NON_SENSITIVE},
        "system.get_time": {},
    }
    OBJECT_FIELD_REGISTRY: dict[str, dict[str, str]] = {
        "proposal_payload": {
            "content": PRIVILEGED_SENSITIVE,
            "body": PRIVILEGED_SENSITIVE,
            "details": SENSITIVE_LOCAL,
            "headers": SENSITIVE_LOCAL,
            "query": SENSITIVE_LOCAL,
            "query_string": SENSITIVE_LOCAL,
            "params": SENSITIVE_LOCAL,
            "rationale": PREVIEW_ONLY,
            "summary": PREVIEW_ONLY,
            "task_details": SENSITIVE_LOCAL,
            "affected_resources": PREVIEW_ONLY,
        },
        "run_request": {
            "objective": PREVIEW_ONLY,
            "file_content": PRIVILEGED_SENSITIVE,
            "http_headers_text": SENSITIVE_LOCAL,
            "http_body": PRIVILEGED_SENSITIVE,
            "task_details": SENSITIVE_LOCAL,
            "system_path": NON_SENSITIVE,
        },
        "summary_payload": {
            "objective": PREVIEW_ONLY,
            "collected": PREVIEW_ONLY,
            "summary_text": PREVIEW_ONLY,
            "operator_summary": PREVIEW_ONLY,
        },
        "connector_input": {
            "path": NON_SENSITIVE,
            "url": NON_SENSITIVE,
            "headers": SENSITIVE_LOCAL,
            "body": PRIVILEGED_SENSITIVE,
            "details": SENSITIVE_LOCAL,
        },
        "connector_output": {
            "content": SENSITIVE_LOCAL,
            "body": PRIVILEGED_SENSITIVE,
            "details": SENSITIVE_LOCAL,
            "summary": PREVIEW_ONLY,
            "tasks": PREVIEW_ONLY,
            "items": PREVIEW_ONLY,
            "entries": PREVIEW_ONLY,
        },
        "agent_input": {
            "objective": PREVIEW_ONLY,
            "request": SENSITIVE_LOCAL,
            "subtasks": PREVIEW_ONLY,
            "collected_keys": NON_SENSITIVE,
            "proposal_count": NON_SENSITIVE,
            "proposal_ids": NON_SENSITIVE,
            "proposal_titles": PREVIEW_ONLY,
            "summary_id": NON_SENSITIVE,
            "intent_summary": PREVIEW_ONLY,
        },
        "agent_output": {
            "operator_summary": PREVIEW_ONLY,
            "intent_summary": PREVIEW_ONLY,
            "subtasks": PREVIEW_ONLY,
            "proposal_ids": NON_SENSITIVE,
            "proposal_titles": PREVIEW_ONLY,
            "result": SENSITIVE_LOCAL,
            "error": PREVIEW_ONLY,
        },
        "handoff_payload": {
            "intent_summary": PREVIEW_ONLY,
            "subtasks": PREVIEW_ONLY,
            "proposal_count": NON_SENSITIVE,
            "proposal_ids": NON_SENSITIVE,
            "summary_id": NON_SENSITIVE,
        },
        "task_node_details": {
            "request": PREVIEW_ONLY,
            "intent_summary": PREVIEW_ONLY,
            "operator_summary": PREVIEW_ONLY,
            "subtasks": PREVIEW_ONLY,
            "proposal_titles": PREVIEW_ONLY,
            "proposal_ids": NON_SENSITIVE,
            "summary_id": NON_SENSITIVE,
            "memory_namespace": NON_SENSITIVE,
            "error": PREVIEW_ONLY,
            "result": PREVIEW_ONLY,
            "connector_output": PREVIEW_ONLY,
        },
        "audit_payload": {
            "objective": PREVIEW_ONLY,
            "reason": PREVIEW_ONLY,
            "error": PREVIEW_ONLY,
            "result": PREVIEW_ONLY,
            "proposal_ids": NON_SENSITIVE,
            "manifest_hash": NON_SENSITIVE,
        },
        "history_payload": {
            "input": PREVIEW_ONLY,
            "output": PREVIEW_ONLY,
            "error_text": PREVIEW_ONLY,
            "result": PREVIEW_ONLY,
        },
        "provider_prompt": {
            "prompt": SENSITIVE_LOCAL,
            "system_prompt": PREVIEW_ONLY,
            "summary": PREVIEW_ONLY,
        },
        "provider_prompt_governance": {
            "prompt_kind": NON_SENSITIVE,
            "selected_variant": NON_SENSITIVE,
            "local_context_classification": NON_SENSITIVE,
            "outbound_classification": NON_SENSITIVE,
            "blocked_sections": PREVIEW_ONLY,
            "sendable_sections": PREVIEW_ONLY,
            "local_prompt_digest": NON_SENSITIVE,
            "remote_prompt_digest": NON_SENSITIVE,
            "routing_mode": NON_SENSITIVE,
        },
    }
    GENERIC_FIELD_CLASSES = {
        "content": PRIVILEGED_SENSITIVE,
        "body": PRIVILEGED_SENSITIVE,
        "details": SENSITIVE_LOCAL,
        "headers": SENSITIVE_LOCAL,
        "query": SENSITIVE_LOCAL,
        "query_string": SENSITIVE_LOCAL,
        "params": SENSITIVE_LOCAL,
        "rationale": PREVIEW_ONLY,
        "summary": PREVIEW_ONLY,
        "summary_text": PREVIEW_ONLY,
        "prompt": SENSITIVE_LOCAL,
        "system_prompt": PREVIEW_ONLY,
        "task_details": SENSITIVE_LOCAL,
        "affected_resources": PREVIEW_ONLY,
    }
    METADATA_SUFFIXES = {
        "_blob_id",
        "_digest",
        "_storage",
        "_storage_class",
        "_encoding",
    }

    def __init__(self, protected_storage: ProtectedStorageService):
        self.protected_storage = protected_storage

    def protect_action_payload(
        self,
        payload: dict[str, Any],
        *,
        classification: DataClassification,
        purpose: str,
        action_type: str | None = None,
        connector: str | None = None,
        object_type: str | None = None,
    ) -> dict[str, Any]:
        protected = deepcopy(payload)
        for key in list(payload):
            storage_class = self.classify_field(
                key,
                action_type=action_type,
                connector=connector,
                object_type=object_type,
            )
            value = protected.get(key)
            if self._is_empty(value):
                continue
            if storage_class in self.STORAGE_FIELDS:
                serialized, encoding = self._serialize_value(value)
                blob = self.protected_storage.store_text_blob(
                    serialized,
                    classification=storage_class,
                    purpose=f"{purpose}:{key}",
                )
                protected.pop(key, None)
                protected[f"{key}_digest"] = blob["digest"]
                protected[f"{key}_blob_id"] = blob["blob_id"]
                protected[f"{key}_storage"] = blob["storage_mode"]
                protected[f"{key}_storage_class"] = storage_class
                protected[f"{key}_encoding"] = encoding
                continue
            if storage_class == self.PREVIEW_ONLY:
                protected[key] = self._preview_value(value)
        return protected

    def materialize_action_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        materialized = deepcopy(payload)
        base_keys = {
            key[: -len("_blob_id")]
            for key in materialized
            if key.endswith("_blob_id")
        }
        for base_key in base_keys:
            blob_id = materialized.get(f"{base_key}_blob_id")
            digest = materialized.get(f"{base_key}_digest")
            encoding = materialized.get(f"{base_key}_encoding", "text")
            if blob_id:
                text = self.protected_storage.load_text_blob(blob_id, expected_digest=digest)
                materialized[base_key] = self._deserialize_value(text, encoding)
        return materialized

    def sanitize_for_history(
        self,
        value: Any,
        *,
        action_type: str | None = None,
        connector: str | None = None,
        object_type: str | None = None,
        field_name: str | None = None,
    ) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, child in value.items():
                if any(key.endswith(suffix) for suffix in self.METADATA_SUFFIXES):
                    redacted[key] = child
                    continue
                if key == "headers" and isinstance(child, dict):
                    redacted[key] = self._sanitize_headers(child)
                    continue
                storage_class = self.classify_field(
                    key,
                    action_type=action_type,
                    connector=connector,
                    object_type=object_type,
                )
                if storage_class in self.STORAGE_FIELDS:
                    redacted[key] = self._redacted_descriptor(child, storage_class)
                    continue
                if storage_class == self.PREVIEW_ONLY:
                    redacted[key] = self._preview_value(child)
                    continue
                redacted[key] = self.sanitize_for_history(
                    child,
                    action_type=action_type,
                    connector=connector,
                    object_type=object_type,
                    field_name=key,
                )
            return redacted
        if isinstance(value, list):
            preview_items = value[:100]
            if field_name and self.classify_field(
                field_name,
                action_type=action_type,
                connector=connector,
                object_type=object_type,
            ) == self.PREVIEW_ONLY:
                return [self._preview_value(item) for item in preview_items]
            return [
                self.sanitize_for_history(
                    item,
                    action_type=action_type,
                    connector=connector,
                    object_type=object_type,
                )
                for item in preview_items
            ]
        if isinstance(value, str):
            if field_name and self.classify_field(
                field_name,
                action_type=action_type,
                connector=connector,
                object_type=object_type,
            ) in self.STORAGE_FIELDS:
                return self._redacted_descriptor(
                    value,
                    self.classify_field(
                        field_name,
                        action_type=action_type,
                        connector=connector,
                        object_type=object_type,
                    ),
                )
            return value[: self.PREVIEW_LIMIT]
        return value

    def sanitize_for_audit(
        self,
        value: Any,
        *,
        action_type: str | None = None,
        connector: str | None = None,
        object_type: str | None = None,
    ) -> Any:
        return self.sanitize_for_history(
            value,
            action_type=action_type,
            connector=connector,
            object_type=object_type,
        )

    def purge_unreferenced_blobs(self, referenced_blob_ids: Iterable[str]) -> int:
        referenced = set(referenced_blob_ids)
        blob_dir = self.protected_storage.base_settings.resolved_protected_blob_dir
        removed = 0
        if not blob_dir.exists():
            return removed
        for blob_file in blob_dir.glob("blob_*.bin"):
            blob_id = blob_file.stem
            if blob_id in referenced:
                continue
            blob_file.unlink(missing_ok=True)
            removed += 1
        return removed

    def classification_overview(
        self,
        payload: dict[str, Any],
        *,
        action_type: str | None = None,
        connector: str | None = None,
        object_type: str | None = None,
    ) -> list[dict[str, str]]:
        overview: list[dict[str, str]] = []
        for key in payload:
            if any(key.endswith(suffix) for suffix in self.METADATA_SUFFIXES):
                continue
            overview.append(
                {
                    "field": key,
                    "storage_class": self.classify_field(
                        key,
                        action_type=action_type,
                        connector=connector,
                        object_type=object_type,
                    ),
                }
            )
        return overview

    def classify_collected_runtime_context(
        self,
        request_payload: dict[str, Any],
        collected: dict[str, Any],
    ) -> tuple[DataClassification, list[str]]:
        reasons: list[str] = []
        restricted = False
        local_only = False

        if request_payload.get("file_content"):
            restricted = True
            reasons.append("explicit file content was supplied")
        if request_payload.get("http_body"):
            restricted = True
            reasons.append("explicit HTTP body content was supplied")
        if request_payload.get("http_headers_text"):
            local_only = True
            reasons.append("operator-supplied HTTP headers may contain local secrets")
        if collected.get("tasks") is not None:
            local_only = True
            reasons.append("runtime task snapshot was collected from the local database")
        if collected.get("filesystem") is not None:
            local_only = True
            reasons.append("local filesystem context was collected")
        if collected.get("system") is not None:
            local_only = True
            reasons.append("bounded system context was collected")

        if restricted:
            return DataClassification.RESTRICTED, reasons
        if local_only:
            return DataClassification.LOCAL_ONLY, reasons
        return DataClassification.EXTERNAL_OK, reasons

    def build_planning_context_views(
        self,
        request_payload: dict[str, Any],
        collected: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        local_classification, reasons = self.classify_collected_runtime_context(request_payload, collected)
        local_context = deepcopy(collected)
        remote_context = {
            "objective": request_payload.get("objective"),
            "request_summary": self._remote_request_summary(request_payload),
            "collected_summary": self._remote_collected_summary(collected),
            "governance_notes": reasons,
        }
        blocked_sections = [
            key for key in ("tasks", "filesystem", "system")
            if key in collected
        ]
        sendable_sections = [
            key for key in remote_context.get("collected_summary", {})
            if remote_context["collected_summary"].get(key) is not None
        ]
        governance = {
            "local_context_classification": local_classification.value,
            "outbound_classification": DataClassification.EXTERNAL_OK.value,
            "blocked_sections": blocked_sections,
            "sendable_sections": sendable_sections,
            "reasons": reasons,
        }
        return local_context, remote_context, governance

    def build_report_context_views(
        self,
        request_payload: dict[str, Any],
        proposals: list[dict[str, Any]],
        summary_text: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        local_context = {
            "objective": request_payload.get("objective"),
            "summary_text": summary_text,
            "reviewed_proposals": proposals,
        }
        remote_context = {
            "objective": request_payload.get("objective"),
            "summary_text": self._preview_value(summary_text),
            "reviewed_proposals": [self._proposal_prompt_view(proposal) for proposal in proposals],
        }
        governance = {
            "local_context_classification": DataClassification.LOCAL_ONLY.value,
            "outbound_classification": DataClassification.EXTERNAL_OK.value,
            "blocked_sections": ["raw_proposal_payloads"],
            "sendable_sections": ["summary_text", "reviewed_proposals"],
            "reasons": ["review output was curated before provider egress"],
        }
        return local_context, remote_context, governance

    def build_prompt_variants(
        self,
        *,
        prompt_kind: str,
        local_prompt: str,
        remote_prompt: str,
        local_classification: DataClassification,
        outbound_classification: DataClassification,
        system_prompt: str | None,
        governance: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "prompt_variants": {
                "local": {
                    "prompt": local_prompt,
                    "system_prompt": system_prompt,
                    "data_classification": local_classification.value,
                    "routing_mode": "local-context",
                },
                "remote": {
                    "prompt": remote_prompt,
                    "system_prompt": system_prompt,
                    "data_classification": outbound_classification.value,
                    "routing_mode": "curated-egress",
                },
            },
            "prompt_governance": {
                "prompt_kind": prompt_kind,
                "local_context_classification": local_classification.value,
                "outbound_classification": outbound_classification.value,
                "blocked_sections": governance.get("blocked_sections", []),
                "sendable_sections": governance.get("sendable_sections", []),
                "reasons": governance.get("reasons", []),
                "local_prompt_digest": sha256_hex(local_prompt),
                "remote_prompt_digest": sha256_hex(remote_prompt),
            },
        }

    def classify_field(
        self,
        field_name: str,
        *,
        action_type: str | None = None,
        connector: str | None = None,
        object_type: str | None = None,
    ) -> str:
        if action_type:
            action_rule = self.ACTION_FIELD_REGISTRY.get(action_type, {})
            if field_name in action_rule:
                return action_rule[field_name]
        if object_type:
            object_rule = self.OBJECT_FIELD_REGISTRY.get(object_type, {})
            if field_name in object_rule:
                return object_rule[field_name]
        if connector and field_name in self.ACTION_FIELD_REGISTRY.get(connector, {}):
            return self.ACTION_FIELD_REGISTRY[connector][field_name]
        return self.GENERIC_FIELD_CLASSES.get(field_name, self.NON_SENSITIVE)

    def collect_blob_ids(self, value: Any) -> set[str]:
        blob_ids: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_blob_id") and isinstance(child, str):
                    blob_ids.add(child)
                    continue
                blob_ids.update(self.collect_blob_ids(child))
            return blob_ids
        if isinstance(value, list):
            for item in value:
                blob_ids.update(self.collect_blob_ids(item))
        return blob_ids

    @staticmethod
    def _serialize_value(value: Any) -> tuple[str, str]:
        if isinstance(value, str):
            return value, "text"
        return json_dumps(value), "json"

    @staticmethod
    def _deserialize_value(raw: str, encoding: str) -> Any:
        if encoding == "json":
            return json.loads(raw)
        return raw

    @staticmethod
    def _is_empty(value: Any) -> bool:
        return value is None or value == "" or value == [] or value == {}

    def _preview_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return value[: self.PREVIEW_LIMIT]
        if isinstance(value, list):
            return [self._preview_value(item) for item in value[:25]]
        if isinstance(value, dict):
            if "headers" in value:
                return self._sanitize_headers(value)
            return {
                key: self._preview_value(child)
                for key, child in list(value.items())[:25]
            }
        return value

    def _redacted_descriptor(self, value: Any, storage_class: str) -> dict[str, Any]:
        serialized, _ = self._serialize_value(value)
        if isinstance(value, dict):
            preview = f"[redacted:{storage_class};keys={len(value)}]"
        elif isinstance(value, list):
            preview = f"[redacted:{storage_class};items={len(value)}]"
        else:
            preview = f"[redacted:{storage_class};len={len(serialized)}]"
        return {
            "redacted": True,
            "storage_class": storage_class,
            "digest": sha256_hex(serialized),
            "length": len(serialized),
            "preview": preview,
            "protection": self.protected_storage.posture_label,
        }

    def _sanitize_headers(self, headers: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in headers.items():
            if key.lower() in self.SENSITIVE_HEADER_KEYS:
                sanitized[key] = {
                    "redacted": True,
                    "digest": sha256_hex(str(value)),
                    "length": len(str(value)),
                }
            else:
                sanitized[key] = self._preview_value(str(value))
        return sanitized

    def _remote_request_summary(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        if request_payload.get("filesystem_path"):
            summary["filesystem_requested"] = True
        if request_payload.get("http_url"):
            summary["http_request"] = {
                "method": (request_payload.get("http_method") or "GET").upper(),
                "target": self._safe_url_summary(request_payload.get("http_url")),
                "body_present": bool(request_payload.get("http_body")),
                "headers_present": bool(request_payload.get("http_headers_text")),
            }
        if request_payload.get("task_title"):
            summary["task_request"] = {
                "title_preview": self._preview_value(request_payload.get("task_title") or ""),
                "details_present": bool(request_payload.get("task_details")),
            }
        if request_payload.get("system_action"):
            summary["system_request"] = {
                "action": request_payload.get("system_action"),
                "path_redacted": bool(request_payload.get("system_path")),
            }
        return summary

    def _remote_collected_summary(self, collected: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        if "tasks" in collected:
            summary["tasks"] = self._summarize_tasks_context(collected.get("tasks") or {})
        if "filesystem" in collected:
            summary["filesystem"] = self._summarize_filesystem_context(collected.get("filesystem") or {})
        if "system" in collected:
            summary["system"] = self._summarize_system_context(collected.get("system") or {})
        if "http" in collected:
            summary["http"] = self._summarize_http_context(collected.get("http") or {})
        return summary

    def _proposal_prompt_view(self, proposal: dict[str, Any]) -> dict[str, Any]:
        payload = proposal.get("payload") or {}
        connector = proposal.get("connector")
        action_type = proposal.get("action_type")
        return {
            "id": proposal.get("id"),
            "title": proposal.get("title"),
            "connector": connector,
            "action_type": action_type,
            "description": proposal.get("description"),
            "risk_level": proposal.get("risk_level"),
            "status": proposal.get("status"),
            "requires_approval": proposal.get("requires_approval"),
            "rationale": self._preview_value(proposal.get("rationale")),
            "policy_notes": [self._preview_value(note) for note in proposal.get("policy_notes") or []],
            "data_classification": proposal.get("data_classification"),
            "payload_preview": self.sanitize_for_history(
                payload,
                action_type=action_type,
                connector=connector,
                object_type="proposal_payload",
            ),
        }

    def _summarize_tasks_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        tasks = payload.get("tasks") or []
        status_counts: dict[str, int] = {}
        for item in tasks:
            status = str((item or {}).get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "task_snapshot_present": True,
            "task_count": len(tasks),
            "status_counts": status_counts,
            "details_redacted": True,
        }

    def _summarize_filesystem_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind") or "unknown"
        summary = {
            "present": True,
            "kind": kind,
            "path_redacted": bool(payload.get("path")),
        }
        if kind == "directory":
            summary["entry_count"] = payload.get("entry_count", len(payload.get("entries") or []))
        if kind == "file":
            summary["size_bytes"] = payload.get("size_bytes")
            summary["preview_redacted"] = True
        if payload.get("error"):
            summary["error"] = self._preview_value(payload.get("error"))
        return summary

    def _summarize_system_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "present": True,
            "path_redacted": bool(payload.get("path")),
            "entries_count": len(payload.get("entries") or []),
            "preview_redacted": "preview" in payload,
            "exists": payload.get("exists"),
            "is_file": payload.get("is_file"),
            "is_dir": payload.get("is_dir"),
        }

    def _summarize_http_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = payload.get("headers") or {}
        content_type = headers.get("content-type") or headers.get("Content-Type")
        return {
            "present": True,
            "target": self._safe_url_summary(payload.get("url")),
            "status_code": payload.get("status_code"),
            "content_type": self._preview_value(str(content_type or "")),
            "body_preview": self._preview_value(payload.get("body_preview") or ""),
        }

    @staticmethod
    def _safe_url_summary(url: str | None) -> dict[str, str] | None:
        if not url:
            return None
        parsed = urlsplit(url)
        return {
            "scheme": parsed.scheme,
            "host": parsed.hostname or "",
            "path": parsed.path[:128],
        }
