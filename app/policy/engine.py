from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from app.config.settings import AppSettings
from app.schemas.actions import ActionProposal, ProposalStatus, RiskLevel, RuntimeMode
from app.services.settings_service import SettingsService


class PolicyEngine:
    SIDE_EFFECT_ACTIONS = {
        "filesystem.write_text",
        "filesystem.append_text",
        "filesystem.delete_path",
        "filesystem.copy_path",
        "filesystem.move_path",
        "filesystem.make_directory",
        "http.post",
        "http.put",
        "http.patch",
        "http.delete",
        "task.create",
        "task.complete",
        "system.powershell",
        "outlook.send_mail",
    }

    def __init__(self, base_settings: AppSettings, settings_service: SettingsService):
        self.base_settings = base_settings
        self.settings_service = settings_service

    def evaluate(self, proposal: ActionProposal) -> ActionProposal:
        settings = self.settings_service.get_effective_settings()
        policy_notes = list(proposal.policy_notes)
        side_effecting = proposal.action_type in self.SIDE_EFFECT_ACTIONS
        risk_level = self._risk_for_action(proposal.action_type, side_effecting)
        status = proposal.status

        block_reason = self._block_reason(proposal, settings)
        if block_reason:
            policy_notes.append(block_reason)
            risk_level = RiskLevel.CRITICAL
            status = ProposalStatus.BLOCKED
        else:
            policy_notes.append(
                f"Mode={settings.runtime_mode.value}; approval gate={'strict' if settings.runtime_mode == RuntimeMode.SAFE else 'risk-based'}."
            )

        requires_approval = settings.runtime_mode == RuntimeMode.SAFE or side_effecting or risk_level in {
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        }

        return proposal.model_copy(
            update={
                "policy_notes": policy_notes,
                "side_effecting": side_effecting,
                "risk_level": risk_level,
                "requires_approval": requires_approval,
                "status": status,
            }
        )

    def validate_execution(self, proposal: ActionProposal) -> None:
        settings = self.settings_service.get_effective_settings()
        block_reason = self._block_reason(proposal, settings)
        if block_reason:
            raise ValueError(block_reason)

    def _block_reason(self, proposal: ActionProposal, settings) -> str | None:
        if proposal.connector == "filesystem":
            return self._validate_filesystem_payload(proposal.payload)
        if proposal.connector == "http":
            return self._validate_http_payload(proposal.payload, settings.allowed_http_hosts)
        if proposal.action_type == "system.powershell":
            return self._validate_powershell_payload(proposal.payload, settings.powershell_allowlist)
        return None

    def _validate_filesystem_payload(self, payload: dict) -> str | None:
        for key in ("path", "source_path", "destination_path"):
            raw_value = payload.get(key)
            if not raw_value:
                continue
            candidate = Path(raw_value).resolve()
            if not any(self._is_relative_to(candidate, root) for root in self._allowed_root_paths()):
                return f"Path {candidate} is outside the filesystem allowlist."
        return None

    def _validate_http_payload(self, payload: dict, allowed_hosts: list[str]) -> str | None:
        url = payload.get("url")
        if not url:
            return "HTTP actions require a URL."
        host = urlparse(url).hostname
        if not host:
            return "HTTP action URL is invalid."
        if allowed_hosts and host not in allowed_hosts:
            return f"Host {host} is not in the HTTP allowlist."
        return None

    @staticmethod
    def _validate_powershell_payload(payload: dict, allowed_commands: list[str]) -> str | None:
        command = (payload.get("command") or "").strip()
        if not command:
            return "PowerShell actions require a command."
        first_token = command.split()[0]
        if allowed_commands and first_token not in allowed_commands:
            return f"PowerShell command {first_token} is not in the allowlist."
        return None

    def _allowed_root_paths(self) -> list[Path]:
        settings = self.settings_service.get_effective_settings()
        roots: list[Path] = []
        for root in settings.allowed_filesystem_roots:
            path = Path(root)
            if not path.is_absolute():
                path = (self.base_settings.project_root / path).resolve()
            else:
                path = path.resolve()
            roots.append(path)
        return roots

    @staticmethod
    def _risk_for_action(action_type: str, side_effecting: bool) -> RiskLevel:
        if action_type in {"filesystem.delete_path", "outlook.send_mail"}:
            return RiskLevel.HIGH
        if action_type == "system.powershell":
            return RiskLevel.HIGH
        if side_effecting:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
