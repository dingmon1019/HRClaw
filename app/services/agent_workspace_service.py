from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config.settings import AppSettings
from app.core.utils import sha256_hex


@dataclass(frozen=True)
class AgentWorkspaceLayout:
    run_id: str
    agent_role: str
    memory_namespace: str
    context_namespace: str
    branch_key: str | None
    shared_workspace_root: Path
    run_root: Path
    agent_root: Path
    scratch_root: Path
    reports_root: Path
    promotion_root: Path

    def as_dict(self, *, promotion_target_hint: str | None = None) -> dict[str, Any]:
        return {
            "scope_model": "shared-workspace + agent-scratch",
            "agent_role": self.agent_role,
            "memory_namespace": self.memory_namespace,
            "context_namespace": self.context_namespace,
            "branch_key": self.branch_key,
            "shared_workspace_root": str(self.shared_workspace_root),
            "run_root": str(self.run_root),
            "agent_root": str(self.agent_root),
            "scratch_root": str(self.scratch_root),
            "reports_root": str(self.reports_root),
            "promotion_root": str(self.promotion_root),
            "promotion_policy": "approved-filesystem-copy-or-move",
            "promotion_note": (
                "Agent scratch paths stay local to the agent context. Promote anything into the shared "
                "workspace only through an approved filesystem.copy_path or filesystem.move_path action."
            ),
            "promotion_target_hint": promotion_target_hint,
        }


class AgentWorkspaceService:
    ROOT_NAME = "agent_workspaces"

    def __init__(self, base_settings: AppSettings):
        self.base_settings = base_settings
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self.base_settings.resolved_runtime_state_root / self.ROOT_NAME

    def layout_for(
        self,
        *,
        run_id: str,
        agent_role: str,
        memory_namespace: str,
        context_namespace: str,
        branch_key: str | None = None,
    ) -> AgentWorkspaceLayout:
        namespace_slug = self._namespace_slug(context_namespace)
        role_root = self.root / run_id / agent_role
        agent_root = role_root / namespace_slug
        scratch_root = agent_root / "scratch"
        reports_root = agent_root / "reports"
        promotion_root = agent_root / "promotion"
        for path in (role_root, agent_root, scratch_root, reports_root, promotion_root):
            path.mkdir(parents=True, exist_ok=True)
        return AgentWorkspaceLayout(
            run_id=run_id,
            agent_role=agent_role,
            memory_namespace=memory_namespace,
            context_namespace=context_namespace,
            branch_key=branch_key,
            shared_workspace_root=self.base_settings.resolved_workspace_root,
            run_root=self.root / run_id,
            agent_root=agent_root,
            scratch_root=scratch_root,
            reports_root=reports_root,
            promotion_root=promotion_root,
        )

    def describe_layout(
        self,
        *,
        run_id: str,
        agent_role: str,
        memory_namespace: str,
        context_namespace: str,
        branch_key: str | None = None,
        promotion_target_hint: str | None = None,
    ) -> dict[str, Any]:
        layout = self.layout_for(
            run_id=run_id,
            agent_role=agent_role,
            memory_namespace=memory_namespace,
            context_namespace=context_namespace,
            branch_key=branch_key,
        )
        return layout.as_dict(promotion_target_hint=promotion_target_hint)

    @staticmethod
    def _namespace_slug(context_namespace: str) -> str:
        safe_prefix = "".join(ch if ch.isalnum() else "-" for ch in context_namespace.lower()).strip("-")
        safe_prefix = safe_prefix[:32] or "agent"
        return f"{safe_prefix}-{sha256_hex(context_namespace)[:10]}"
