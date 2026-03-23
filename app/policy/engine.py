from __future__ import annotations

from app.config.settings import AppSettings
from app.connectors.system import SAFE_SYSTEM_ACTIONS
from app.core.errors import ConnectorError
from app.policy.path_guard import PathGuard
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
        "outlook.send_mail",
    }

    def __init__(self, base_settings: AppSettings, settings_service: SettingsService):
        self.base_settings = base_settings
        self.settings_service = settings_service
        self.path_guard = PathGuard(base_settings, settings_service)

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
            write = proposal.action_type not in {"filesystem.list_directory", "filesystem.read_text"}
            return self.path_guard.check_payload(proposal.payload, write=write)
        if proposal.connector == "http":
            return self._validate_http_payload(proposal.payload, proposal.action_type)
        if proposal.connector == "system":
            return self._validate_system_action(proposal.action_type, proposal.payload, settings.enable_system_connector)
        if proposal.connector == "outlook" and not settings.enable_outlook_connector:
            return "Outlook connector is disabled."
        return None

    def _validate_http_payload(self, payload: dict, action_type: str) -> str | None:
        url = payload.get("url")
        if not url:
            return "HTTP actions require a URL."
        try:
            from app.connectors.http import HttpConnector

            HttpConnector(self.settings_service)._assert_request_allowed(url, action_type.split(".", 1)[1].upper())
            return None
        except ConnectorError as exc:
            return str(exc)

    @staticmethod
    def _validate_system_action(action_type: str, payload: dict, enabled: bool) -> str | None:
        if not enabled:
            return "Bounded system connector is disabled."
        if action_type not in SAFE_SYSTEM_ACTIONS:
            return f"System action {action_type} is not allowed."
        if action_type != "system.get_time" and not payload.get("path"):
            return "System action requires a path."
        return None

    @staticmethod
    def _risk_for_action(action_type: str, side_effecting: bool) -> RiskLevel:
        if action_type in {"filesystem.delete_path", "outlook.send_mail", "http.delete"}:
            return RiskLevel.HIGH
        if action_type in {"http.post", "http.put", "http.patch", "filesystem.move_path"}:
            return RiskLevel.MEDIUM
        if side_effecting:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW
