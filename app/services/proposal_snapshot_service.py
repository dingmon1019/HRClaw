from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.utils import canonical_directory_digest, json_dumps, json_loads, new_id, sha256_file, sha256_hex, utcnow_iso
from app.policy.path_guard import PathGuard
from app.schemas.actions import ApprovalRecord, ProposalRecord, ProposalSnapshotRecord
from app.services.data_governance_service import DataGovernanceService
from app.services.settings_service import SettingsService


class ProposalSnapshotService:
    def __init__(
        self,
        base_settings: AppSettings,
        database: Database,
        settings_service: SettingsService,
        data_governance_service: DataGovernanceService,
    ):
        self.base_settings = base_settings
        self.database = database
        self.settings_service = settings_service
        self.path_guard = PathGuard(base_settings, settings_service)
        self.data_governance_service = data_governance_service

    def capture(self, proposal: ProposalRecord, status: str) -> ProposalSnapshotRecord:
        runtime_payload = self.data_governance_service.materialize_action_payload(proposal.payload)
        before_state, preview = self._safe_before_state_and_preview(proposal)
        action_hash = sha256_hex(f"{proposal.action_type}|{json_dumps(runtime_payload)}")
        policy_hash = sha256_hex(
            json_dumps(
                {
                    "risk_level": proposal.risk_level.value,
                    "requires_approval": proposal.requires_approval,
                    "policy_notes": proposal.policy_notes,
                    "data_classification": proposal.data_classification.value,
                }
            )
        )
        settings_hash = self.settings_service.current_settings_hash()
        resource_hash = sha256_hex(json_dumps(before_state))
        manifest = self._build_manifest(
            proposal,
            action_hash=action_hash,
            policy_hash=policy_hash,
            settings_hash=settings_hash,
            resource_hash=resource_hash,
        )
        manifest_hash = sha256_hex(json_dumps(manifest))
        snapshot_hash = manifest_hash
        record = ProposalSnapshotRecord(
            id=new_id("snapshot"),
            proposal_id=proposal.id,
            snapshot_hash=snapshot_hash,
            action_hash=action_hash,
            policy_hash=policy_hash,
            settings_hash=settings_hash,
            resource_hash=resource_hash,
            manifest_hash=manifest_hash,
            manifest=manifest,
            before_state=before_state,
            preview=preview,
            comparison_json={},
            stale_reason=None,
            status=status,
            created_at=utcnow_iso(),
        )
        self.database.execute(
            """
            INSERT INTO proposal_snapshots(
                id, proposal_id, snapshot_hash, action_hash, policy_hash, settings_hash, resource_hash,
                manifest_hash, manifest_json,
                before_state_json, preview_json, comparison_json, stale_reason, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.proposal_id,
                record.snapshot_hash,
                record.action_hash,
                record.policy_hash,
                record.settings_hash,
                record.resource_hash,
                record.manifest_hash,
                json_dumps(record.manifest),
                json_dumps(record.before_state),
                json_dumps(record.preview),
                json_dumps(record.comparison_json),
                record.stale_reason,
                record.status,
                record.created_at,
            ),
        )
        self.database.execute(
            "UPDATE proposals SET snapshot_hash = ?, stale_reason = NULL, updated_at = ? WHERE id = ?",
            (record.snapshot_hash, record.created_at, proposal.id),
        )
        return record

    def compare_live_to_latest(self, proposal: ProposalRecord, status: str | None = None) -> dict[str, Any]:
        baseline = self.latest(proposal.id, status=status)
        if baseline is None and status == "approved":
            baseline = self.latest(proposal.id, status="created")
        live = self._build_live_snapshot(proposal)
        if baseline is None:
            return {
                "baseline": None,
                "live": live,
                "stale": False,
                "reason": None,
                "changed_fields": [],
            }
        changed_fields: list[str] = []
        if baseline.action_hash != live.action_hash:
            changed_fields.append("action")
        if baseline.policy_hash != live.policy_hash:
            changed_fields.append("policy")
        if baseline.settings_hash != live.settings_hash:
            changed_fields.append("settings")
        if baseline.resource_hash != live.resource_hash:
            changed_fields.append("resources")
        stale = bool(changed_fields)
        reason = None
        if stale:
            reason = f"Snapshot drift detected in {', '.join(changed_fields)}."
        live.comparison_json = {
            "changed_fields": changed_fields,
            "baseline_snapshot_hash": baseline.snapshot_hash,
            "live_snapshot_hash": live.snapshot_hash,
            "baseline_manifest_hash": baseline.manifest_hash,
            "live_manifest_hash": live.manifest_hash,
        }
        return {
            "baseline": baseline,
            "live": live,
            "stale": stale,
            "reason": reason,
            "changed_fields": changed_fields,
        }

    def mark_stale(self, proposal_id: str, reason: str) -> None:
        self.database.execute(
            "UPDATE proposals SET status = ?, stale_reason = ?, updated_at = ? WHERE id = ?",
            ("stale", reason, utcnow_iso(), proposal_id),
        )

    def latest(self, proposal_id: str, status: str | None = None) -> ProposalSnapshotRecord | None:
        if status:
            row = self.database.fetch_one(
                """
                SELECT * FROM proposal_snapshots
                WHERE proposal_id = ? AND status = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (proposal_id, status),
            )
        else:
            row = self.database.fetch_one(
                "SELECT * FROM proposal_snapshots WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1",
                (proposal_id,),
            )
        return self._row_to_record(row) if row else None

    def list_for_proposal(self, proposal_id: str) -> list[ProposalSnapshotRecord]:
        rows = self.database.fetch_all(
            "SELECT * FROM proposal_snapshots WHERE proposal_id = ? ORDER BY created_at DESC",
            (proposal_id,),
        )
        return [self._row_to_record(row) for row in rows]

    def verify_or_raise(self, proposal: ProposalRecord) -> ProposalSnapshotRecord:
        comparison = self.compare_live_to_latest(proposal, status="approved")
        if comparison["stale"]:
            reason = comparison["reason"] or "Approved snapshot no longer matches live state."
            self.mark_stale(proposal.id, reason)
            raise ValueError(reason)
        return comparison["live"]

    def compare_live_to_approval(
        self,
        proposal: ProposalRecord,
        approval: ApprovalRecord,
    ) -> dict[str, Any]:
        live = self._build_live_snapshot(proposal)
        changed_fields: list[str] = []
        if approval.action_hash != live.action_hash:
            changed_fields.append("action")
        if approval.policy_hash != live.policy_hash:
            changed_fields.append("policy")
        if approval.settings_hash != live.settings_hash:
            changed_fields.append("settings")
        if approval.resource_hash != live.resource_hash:
            changed_fields.append("resources")
        stale = bool(changed_fields)
        reason = None
        if stale:
            reason = (
                "Snapshot drift detected against the queued approval in "
                f"{', '.join(changed_fields)}."
            )
        live.comparison_json = {
            "changed_fields": changed_fields,
            "approval_snapshot_hash": approval.snapshot_hash,
            "live_snapshot_hash": live.snapshot_hash,
            "approval_manifest_hash": approval.manifest_hash,
            "live_manifest_hash": live.manifest_hash,
        }
        return {
            "approval": approval,
            "live": live,
            "stale": stale,
            "reason": reason,
            "changed_fields": changed_fields,
        }

    def verify_approval_or_raise(
        self,
        proposal: ProposalRecord,
        approval: ApprovalRecord,
    ) -> ProposalSnapshotRecord:
        if approval.decision != "approved":
            raise ValueError("Execution job is not bound to an approved decision.")
        if not all(
            [
                approval.snapshot_hash,
                approval.action_hash,
                approval.policy_hash,
                approval.settings_hash,
                approval.resource_hash,
                approval.manifest_hash,
            ]
        ):
            raise ValueError("Approval record is missing immutable snapshot binding hashes.")
        comparison = self.compare_live_to_approval(proposal, approval)
        if comparison["stale"]:
            reason = comparison["reason"] or "Queued approval no longer matches live state."
            self.mark_stale(proposal.id, reason)
            raise ValueError(reason)
        return comparison["live"]

    def _build_live_snapshot(self, proposal: ProposalRecord) -> ProposalSnapshotRecord:
        runtime_payload = self.data_governance_service.materialize_action_payload(proposal.payload)
        before_state, preview = self._safe_before_state_and_preview(proposal)
        action_hash = sha256_hex(f"{proposal.action_type}|{json_dumps(runtime_payload)}")
        policy_hash = sha256_hex(
            json_dumps(
                {
                    "risk_level": proposal.risk_level.value,
                    "requires_approval": proposal.requires_approval,
                    "policy_notes": proposal.policy_notes,
                    "data_classification": proposal.data_classification.value,
                }
            )
        )
        settings_hash = self.settings_service.current_settings_hash()
        resource_hash = sha256_hex(json_dumps(before_state))
        manifest = self._build_manifest(
            proposal,
            action_hash=action_hash,
            policy_hash=policy_hash,
            settings_hash=settings_hash,
            resource_hash=resource_hash,
        )
        manifest_hash = sha256_hex(json_dumps(manifest))
        snapshot_hash = manifest_hash
        return ProposalSnapshotRecord(
            id="live",
            proposal_id=proposal.id,
            snapshot_hash=snapshot_hash,
            action_hash=action_hash,
            policy_hash=policy_hash,
            settings_hash=settings_hash,
            resource_hash=resource_hash,
            manifest_hash=manifest_hash,
            manifest=manifest,
            before_state=before_state,
            preview=preview,
            comparison_json={},
            stale_reason=None,
            status="live",
            created_at=utcnow_iso(),
        )

    def _before_state_and_preview(self, proposal: ProposalRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime_payload = self.data_governance_service.materialize_action_payload(proposal.payload)
        runtime_proposal = proposal.model_copy(update={"payload": runtime_payload})
        if runtime_proposal.connector == "filesystem":
            return self._filesystem_state(runtime_proposal)
        if runtime_proposal.connector == "http":
            method = runtime_proposal.action_type.split(".", 1)[1].upper()
            headers = runtime_proposal.payload.get("headers") or {}
            body = runtime_proposal.payload.get("body") or ""
            before_state = {
                "method": method,
                "url": runtime_proposal.payload.get("url"),
                "headers": headers,
                "body_digest": sha256_hex(body),
                "follow_redirects": self.settings_service.get_effective_settings().http_follow_redirects,
            }
            return before_state, {"http_request": before_state, "body_preview": body[:2000]}
        if runtime_proposal.connector == "system":
            before_state = {
                "action": runtime_proposal.action_type,
                "path": runtime_proposal.payload.get("path"),
            }
            return before_state, {"system_action": before_state}
        if runtime_proposal.connector == "task":
            before_state = runtime_proposal.payload
            return before_state, {"task_payload": runtime_proposal.payload}
        return runtime_proposal.payload, {"payload": runtime_proposal.payload}

    def _safe_before_state_and_preview(self, proposal: ProposalRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return self._before_state_and_preview(proposal)
        except Exception as exc:
            return (
                {"error": str(exc), "payload": proposal.payload, "connector": proposal.connector},
                {"preview_error": str(exc)},
            )

    def _filesystem_state(self, proposal: ProposalRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        raw_path = proposal.payload.get("path")
        if not raw_path:
            return {"path": None, "exists": False}, {}
        resolved = self.path_guard.resolve_for_probe(raw_path)
        exists = resolved.exists()
        state: dict[str, Any] = {
            "path": str(resolved),
            "exists": exists,
            "kind": "missing",
        }
        preview: dict[str, Any] = {"target_path": str(resolved)}
        if not exists:
            return state, preview
        stat = resolved.stat()
        state["mtime_ns"] = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        state["size_bytes"] = stat.st_size
        if resolved.is_dir():
            entries = sorted(child.name for child in resolved.iterdir())[:100]
            state["kind"] = "directory"
            state["entries"] = entries
            state["tree_digest"] = canonical_directory_digest(resolved)
            preview["before_preview"] = json.dumps(entries, indent=2)
            return state, preview
        text = resolved.read_text(encoding="utf-8", errors="ignore")
        limited_text = text[: self.settings_service.get_effective_settings().filesystem_max_read_bytes]
        state["kind"] = "file"
        state["content_hash"] = sha256_file(resolved)
        preview["before_preview"] = limited_text
        if proposal.action_type in {"filesystem.write_text", "filesystem.append_text"}:
            incoming = proposal.payload.get("content", "")
            after_text = incoming if proposal.action_type == "filesystem.write_text" else limited_text + incoming
            preview["after_preview"] = after_text[: self.settings_service.get_effective_settings().filesystem_max_read_bytes]
            preview["diff_preview"] = "\n".join(
                difflib.unified_diff(
                    limited_text.splitlines(),
                    after_text.splitlines(),
                    fromfile="before",
                    tofile="after",
                    lineterm="",
                )
            )
        return state, preview

    @staticmethod
    def _build_manifest(
        proposal: ProposalRecord,
        *,
        action_hash: str,
        policy_hash: str,
        settings_hash: str,
        resource_hash: str,
    ) -> dict[str, Any]:
        return {
            "connector": proposal.connector,
            "action_type": proposal.action_type,
            "created_by_agent_id": proposal.created_by_agent_id,
            "created_by_agent_role": proposal.created_by_agent_role,
            "reviewed_by_agent_id": proposal.reviewed_by_agent_id,
            "reviewed_by_agent_role": proposal.reviewed_by_agent_role,
            "action_hash": action_hash,
            "policy_hash": policy_hash,
            "settings_hash": settings_hash,
            "resource_hash": resource_hash,
        }

    @staticmethod
    def _row_to_record(row) -> ProposalSnapshotRecord:
        return ProposalSnapshotRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            snapshot_hash=row["snapshot_hash"],
            action_hash=row["action_hash"],
            policy_hash=row["policy_hash"],
            settings_hash=row["settings_hash"],
            resource_hash=row["resource_hash"],
            manifest_hash=row["manifest_hash"] or row["snapshot_hash"],
            manifest=json_loads(row["manifest_json"], {}),
            before_state=json_loads(row["before_state_json"], {}),
            preview=json_loads(row["preview_json"], {}),
            comparison_json=json_loads(row["comparison_json"], {}),
            stale_reason=row["stale_reason"],
            status=row["status"],
            created_at=row["created_at"],
        )
