from __future__ import annotations

import difflib
import json
from collections import Counter
from typing import Any
from urllib.parse import urlencode

from fastapi.encoders import jsonable_encoder
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.dependencies import get_container, get_templates
from app.core.container import AppContainer
from app.core.errors import (
    AuthenticationError,
    AuthorizationError,
    CsrfError,
    InvalidStateError,
    NotFoundError,
    RateLimitError,
)
from app.schemas.actions import (
    ApprovalDecisionRequest,
    AgentRunRequest,
    ProposalRecord,
    ProposalStatus,
    RiskLevel,
)
from app.schemas.auth import SetupRequest
from app.schemas.providers import ProviderTestRequest
from app.schemas.settings import SanitizedSettingsExport, SettingsUpdate
from app.security.auth import (
    login_session,
    logout_session,
    mark_recent_auth,
    read_session_user,
    touch_session_activity,
)
from app.security.csrf import ensure_csrf_token, validate_csrf


router = APIRouter()

HIGH_RISK_LEVELS = {RiskLevel.HIGH, RiskLevel.CRITICAL}
PENDING_STATES = {ProposalStatus.PENDING, ProposalStatus.APPROVED, ProposalStatus.QUEUED, ProposalStatus.RUNNING}


def _redirect(path: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {key: value for key, value in {"message": message, "error": error}.items() if value}
    url = path if not params else f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "local"


def _session_user(request: Request, container: AppContainer):
    if not container.auth_service.has_users():
        return None
    record = container.session_service.get_active(request.session.get("session_id"))
    if record is None:
        logout_session(request, container.session_service)
        return None
    if container.session_service.is_idle_expired(record):
        logout_session(request, container.session_service)
        return None
    record = touch_session_activity(request, container.session_service) or record
    return read_session_user(record, recent_auth=container.session_service.has_recent_auth(record))


def _page_user_or_redirect(request: Request, container: AppContainer):
    if not container.auth_service.has_users():
        return _redirect("/setup")
    user = _session_user(request, container)
    if user is None:
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        return _redirect(f"/login?{urlencode({'next': next_path})}")
    return user


def _api_user_or_401(request: Request, container: AppContainer):
    if not container.auth_service.has_users():
        raise HTTPException(status_code=403, detail="Initial setup has not been completed.")
    user = _session_user(request, container)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_recent_auth(
    request: Request,
    container: AppContainer,
    user,
    current_password: str | None,
    purpose: str,
) -> None:
    if user.recent_auth:
        return
    if not current_password:
        raise AuthorizationError(f"Recent re-authentication is required to {purpose}.")
    container.auth_service.verify_current_password(user.id, current_password)
    mark_recent_auth(request, container.session_service)
    container.audit_service.emit("auth.reauth", {"username": user.username, "purpose": purpose})


def _render(
    templates: Jinja2Templates,
    request: Request,
    name: str,
    container: AppContainer,
    **context: Any,
):
    base_context = {
        "request": request,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        "csrf_token": ensure_csrf_token(request),
        "current_user": _session_user(request, container),
        "auth_configured": container.auth_service.has_users(),
    }
    base_context.update(context)
    return templates.TemplateResponse(request=request, name=name, context=base_context)


def _filter_proposals(
    proposals: list[ProposalRecord],
    status_filter: str | None,
    risk_filter: str | None,
    connector_filter: str | None,
    run_id: str | None,
) -> list[ProposalRecord]:
    filtered = proposals
    if status_filter:
        filtered = [proposal for proposal in filtered if proposal.status.value == status_filter]
    if risk_filter:
        filtered = [proposal for proposal in filtered if proposal.risk_level.value == risk_filter]
    if connector_filter:
        filtered = [proposal for proposal in filtered if proposal.connector == connector_filter]
    if run_id:
        filtered = [proposal for proposal in filtered if proposal.run_id == run_id]
    return filtered


def _proposal_resources(proposal: ProposalRecord) -> list[str]:
    resources: list[str] = []
    for key in ("path", "source_path", "destination_path", "url", "title", "task_id"):
        value = proposal.payload.get(key)
        if value:
            resources.append(f"{key}: {value}")
    if not resources:
        resources.append("No external resource reference captured.")
    return resources


def _preview_text(value: str, limit: int = 2500) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n\n... truncated ..."


def _filesystem_preview(container: AppContainer, proposal: ProposalRecord) -> dict[str, Any]:
    path_guard = container.policy_engine.path_guard
    action_type = proposal.action_type
    raw_path = proposal.payload.get("path")
    runtime_payload = container.data_governance_service.materialize_action_payload(proposal.payload)
    if not raw_path:
        return {}
    try:
        path = path_guard.resolve_for_probe(raw_path)
    except Exception as exc:
        return {"preview_error": str(exc)}

    before_text = ""
    if path.exists() and path.is_file():
        before_text = path.read_text(encoding="utf-8", errors="ignore")
    preview: dict[str, Any] = {"target_path": str(path)}
    if action_type == "filesystem.read_text":
        preview["before_preview"] = _preview_text(before_text)
        return preview
    if action_type == "filesystem.list_directory":
        entries = sorted(child.name for child in path.iterdir())[:50] if path.exists() and path.is_dir() else []
        preview["before_preview"] = json.dumps(entries, indent=2)
        return preview
    if action_type in {"filesystem.write_text", "filesystem.append_text"}:
        incoming = runtime_payload.get("content", "")
        after_text = incoming if action_type == "filesystem.write_text" else before_text + incoming
        diff = "\n".join(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
            )
        )
        preview["before_preview"] = _preview_text(before_text or "(file did not previously exist)")
        preview["after_preview"] = _preview_text(after_text)
        preview["diff_preview"] = _preview_text(diff or "(no textual diff)")
        return preview
    if action_type == "filesystem.delete_path":
        preview["before_preview"] = _preview_text(before_text or "(directory or missing path)")
        return preview
    return preview


def _rollback_indicator(proposal: ProposalRecord) -> str:
    mapping = {
        "filesystem.write_text": "Manual rollback only. No automatic restore snapshot is stored.",
        "filesystem.append_text": "Manual rollback only. Diff preview is available before approval.",
        "filesystem.delete_path": "No automatic rollback. Delete actions should be treated as destructive.",
        "filesystem.make_directory": "Manual rollback possible by removing the created directory.",
        "http.post": "Rollback depends entirely on the remote service.",
        "http.put": "Rollback depends entirely on the remote service.",
        "http.patch": "Rollback depends entirely on the remote service.",
        "http.delete": "Rollback depends entirely on the remote service.",
        "task.create": "Manual rollback is possible by marking or removing the task later.",
    }
    return mapping.get(proposal.action_type, "No automatic rollback capability is implemented.")


def _proposal_preview(container: AppContainer, proposal: ProposalRecord) -> dict[str, Any]:
    runtime_payload = container.data_governance_service.materialize_action_payload(proposal.payload)
    preview: dict[str, Any] = {
        "affected_resources": _proposal_resources(proposal),
        "execution_preview": container.data_governance_service.sanitize_for_history(runtime_payload),
        "rollback": _rollback_indicator(proposal),
    }
    if proposal.connector == "filesystem":
        preview.update(_filesystem_preview(container, proposal))
    elif proposal.connector == "http":
        preview["http_preview"] = {
            "method": proposal.action_type.split(".", 1)[1].upper(),
            "url": proposal.payload.get("url"),
            "headers": proposal.payload.get("headers") or {},
            "body": runtime_payload.get("body"),
        }
    elif proposal.connector == "system":
        preview["system_preview"] = {
            "action": proposal.action_type,
            "path": proposal.payload.get("path"),
        }
    return preview


def _proposal_snapshot_context(container: AppContainer, proposal: ProposalRecord) -> dict[str, Any]:
    created_snapshot = container.proposal_snapshot_service.latest(proposal.id, status="created")
    approved_snapshot = container.proposal_snapshot_service.latest(proposal.id, status="approved")
    live_against_approval = container.proposal_snapshot_service.compare_live_to_latest(
        proposal,
        status="approved",
    )
    preview = _proposal_preview(container, proposal)
    if live_against_approval["live"]:
        preview.update(live_against_approval["live"].preview)
    return {
        "created_snapshot": created_snapshot,
        "approved_snapshot": approved_snapshot,
        "live_snapshot": live_against_approval["live"],
        "stale": live_against_approval["stale"],
        "stale_reason": proposal.stale_reason or live_against_approval["reason"],
        "changed_fields": live_against_approval["changed_fields"],
        "preview": preview,
    }


def _run_context(container: AppContainer, run_id: str) -> dict[str, Any]:
    summary = container.summary_service.get_by_run_id(run_id)
    proposals = [proposal for proposal in container.proposal_service.list() if proposal.run_id == run_id]
    agent_runs = container.agent_service.list_run_history(run_id)
    handoffs = container.agent_service.list_handoffs(run_id)
    connector_runs = [entry for entry in container.history_service.list_connector_runs(limit=200) if entry.run_id == run_id]
    return {
        "summary": summary,
        "proposals": proposals,
        "agent_runs": agent_runs,
        "handoffs": handoffs,
        "connector_runs": connector_runs,
    }


def _unsafe_warnings(container: AppContainer) -> list[str]:
    settings = container.settings_service.get_effective_settings()
    warnings: list[str] = []
    project_root = container.base_settings.project_root.resolve()
    runtime_root = container.base_settings.resolved_runtime_state_root
    if settings.runtime_mode.value == "relaxed":
        warnings.append("Runtime mode is relaxed. More actions can move through with lighter gating.")
    if runtime_root == project_root or project_root in runtime_root.parents:
        warnings.append("Runtime state root is still inside the repository tree. Move it under LocalAppData for safer release hygiene.")
    if settings.allow_http_private_network:
        warnings.append("HTTP private-network access is enabled. Localhost and private IP targets are reachable.")
    if settings.http_follow_redirects:
        warnings.append("HTTP redirects are enabled. Redirect chains can expand the effective request target.")
    if settings.enable_outlook_connector:
        warnings.append("Outlook connector is enabled. Email side effects can leave the local workstation.")
    if not settings.json_audit_enabled:
        warnings.append("JSON audit logging is disabled. Tamper-evident audit visibility is reduced.")
    if settings.allow_provider_private_network:
        warnings.append("Provider private-network egress is enabled. Local LLM endpoints can receive prompts.")
    if settings.allow_restricted_provider_egress:
        warnings.append("Restricted data egress to remote providers is enabled. Review provider trust carefully.")
    if container.protected_storage.storage_mode != "dpapi":
        warnings.append("Sensitive local storage is using plain-local protection. Install pywin32 or choose a stronger host secret store.")
    return warnings


def _temporary_settings(
    container: AppContainer,
    *,
    provider_name: str | None,
    model_name: str | None,
    base_url: str | None,
    api_key_env: str | None,
    generic_http_endpoint: str | None,
    provider_timeout_seconds: float | None,
):
    settings = container.settings_service.get_effective_settings()
    data = settings.model_dump()
    if provider_name:
        data["provider"] = provider_name
    if model_name:
        data["model"] = model_name
    if base_url is not None:
        data["base_url"] = base_url or None
    if api_key_env:
        data["api_key_env"] = api_key_env
    if generic_http_endpoint is not None:
        data["generic_http_endpoint"] = generic_http_endpoint or None
    if provider_timeout_seconds is not None:
        data["provider_timeout_seconds"] = provider_timeout_seconds
    return type(settings)(**data)


def _settings_page_context(container: AppContainer, *, result=None) -> dict[str, Any]:
    settings = container.settings_service.get_effective_settings()
    return {
        "title": "Settings",
        "settings": settings,
        "protected_storage_mode": container.protected_storage.storage_mode,
        "providers": container.provider_service.list_statuses(),
        "unsafe_warnings": _unsafe_warnings(container),
        "audit_status": container.audit_service.verify_integrity(),
        "export_json": json.dumps(
            container.settings_service.export_sanitized().model_dump(mode="json"),
            indent=2,
        ),
        "result": result,
    }


@router.get("/setup")
def setup_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    if container.auth_service.has_users():
        return _redirect("/login")
    return _render(templates, request, "templates/setup.html", container, title="Initial Setup")


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    container: AppContainer = Depends(get_container),
):
    await validate_csrf(request)
    if container.auth_service.has_users():
        return _redirect("/login", error="Initial setup has already been completed.")
    payload = SetupRequest(username=username, password=password, confirm_password=confirm_password)
    if payload.password != payload.confirm_password:
        return _redirect("/setup", error="Passwords do not match.")
    user = container.auth_service.create_initial_user(payload.username, payload.password)
    login_session(
        request,
        user,
        session_service=container.session_service,
        client_ip=_client_key(request),
        user_agent=request.headers.get("user-agent"),
    )
    container.audit_service.emit("auth.bootstrap_completed", {"username": user.username})
    return _redirect("/", message="Initial operator account created.")


@router.get("/login")
def login_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    if not container.auth_service.has_users():
        return _redirect("/setup")
    if _session_user(request, container):
        return _redirect("/")
    return _render(
        templates,
        request,
        "templates/login.html",
        container,
        title="Operator Login",
        next_path=request.query_params.get("next") or "/",
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_path: str = Form("/"),
    container: AppContainer = Depends(get_container),
):
    await validate_csrf(request)
    settings = container.base_settings
    container.rate_limiter.check(
        f"login:{_client_key(request)}",
        settings.login_rate_limit_attempts,
        settings.login_rate_limit_window_seconds,
    )
    user = container.auth_service.authenticate(username, password)
    login_session(
        request,
        user,
        session_service=container.session_service,
        client_ip=_client_key(request),
        user_agent=request.headers.get("user-agent"),
    )
    container.audit_service.emit("auth.login", {"username": user.username, "client": _client_key(request)})
    safe_next = next_path if next_path.startswith("/") else "/"
    return _redirect(safe_next or "/", message="Authenticated.")


@router.post("/logout")
async def logout_submit(request: Request, container: AppContainer = Depends(get_container)):
    await validate_csrf(request)
    user = _session_user(request, container)
    if user:
        container.audit_service.emit("auth.logout", {"username": user.username})
    logout_session(request, container.session_service)
    return _redirect("/login", message="Signed out.")


@router.get("/")
def dashboard(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user

    proposals = container.proposal_service.list()
    counts = Counter(proposal.status.value for proposal in proposals)
    pending_by_risk = Counter(
        proposal.risk_level.value for proposal in proposals if proposal.status in PENDING_STATES
    )
    recent_history = container.history_service.list_action_history(limit=20)
    recent_summaries = container.summary_service.list_recent(limit=5)
    recent_jobs = container.execution_queue_service.list_recent(limit=10)
    recent_high_risk = [proposal for proposal in proposals if proposal.risk_level in HIGH_RISK_LEVELS][:8]
    failed_executions = [entry for entry in recent_history if entry.status in {"failed", "blocked"}][:8]
    audit_status = container.audit_service.verify_integrity()
    recent_runs = container.agent_service.recent_runs(limit=6)
    active_runs = [
        run
        for run in recent_runs
        if any(step.status == "running" for step in container.agent_service.list_run_history(run["run_id"]))
    ]
    context = {
        "title": "Dashboard",
        "counts": counts,
        "pending_by_risk": pending_by_risk,
        "recent_history": recent_history[:10],
        "recent_failed_history": failed_executions,
        "recent_jobs": recent_jobs,
        "recent_summaries": recent_summaries,
        "recent_high_risk": recent_high_risk,
        "providers": container.provider_service.list_statuses(),
        "connectors": container.connector_registry.list_health(),
        "audit_status": audit_status,
        "unsafe_warnings": _unsafe_warnings(container),
        "blocked_count": counts["blocked"],
        "failed_count": counts["failed"],
        "recent_runs": recent_runs,
        "active_runs": active_runs,
    }
    return _render(templates, request, "templates/dashboard.html", container, **context)


@router.get("/run")
def run_agent_form(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    context = {
        "title": "Run Agent",
        "providers": container.provider_service.list_statuses(),
        "settings": container.settings_service.get_effective_settings(),
        "system_actions": [
            "system.list_directory",
            "system.read_text_file",
            "system.test_path",
            "system.get_time",
        ],
        "unsafe_warnings": _unsafe_warnings(container),
    }
    return _render(templates, request, "templates/run_agent.html", container, **context)


@router.post("/run")
async def run_agent_submit(
    request: Request,
    objective: str = Form(...),
    filesystem_path: str | None = Form(None),
    file_content: str | None = Form(None),
    http_url: str | None = Form(None),
    http_method: str = Form("GET"),
    http_body: str | None = Form(None),
    http_headers_text: str | None = Form(None),
    task_title: str | None = Form(None),
    task_details: str | None = Form(None),
    system_action: str | None = Form(None),
    system_path: str | None = Form(None),
    provider_name: str | None = Form(None),
    model_name: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    await validate_csrf(request)
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user

    run_request = AgentRunRequest(
        objective=objective,
        filesystem_path=filesystem_path or None,
        file_content=file_content if file_content not in {"", None} else None,
        http_url=http_url or None,
        http_method=http_method,
        http_body=http_body if http_body not in {"", None} else None,
        http_headers_text=http_headers_text if http_headers_text not in {"", None} else None,
        task_title=task_title or None,
        task_details=task_details or None,
        system_action=system_action or None,
        system_path=system_path or None,
        provider_name=provider_name or None,
        model_name=model_name or None,
    )
    result = container.runtime_service.run_agent(run_request)
    return _redirect(
        f"/runs/{result.run_id}",
        message="Objective decomposed, reviewed, and prepared for approval.",
    )


@router.get("/runs/{run_id}")
def run_detail_page(
    run_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    context = _run_context(container, run_id)
    if context["summary"] is None:
        raise NotFoundError(f"Run {run_id} was not found.")
    context.update(
        {
            "title": "Assistant Workbench",
            "run_id": run_id,
            "high_risk_count": sum(1 for proposal in context["proposals"] if proposal.risk_level in HIGH_RISK_LEVELS),
            "pending_count": sum(
                1
                for proposal in context["proposals"]
                if proposal.status in {ProposalStatus.PENDING, ProposalStatus.APPROVED, ProposalStatus.QUEUED, ProposalStatus.RUNNING}
            ),
        }
    )
    return _render(templates, request, "templates/run_detail.html", container, **context)


@router.get("/proposals")
def proposals_page(
    request: Request,
    status_filter: str | None = None,
    risk_filter: str | None = None,
    connector_filter: str | None = None,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    proposals = _filter_proposals(
        container.proposal_service.list(),
        status_filter,
        risk_filter,
        connector_filter,
        request.query_params.get("run_id"),
    )
    context = {
        "title": "Proposals Inbox",
        "proposals": proposals,
        "status_filter": status_filter,
        "risk_filter": risk_filter,
        "connector_filter": connector_filter,
        "high_risk_count": sum(1 for proposal in proposals if proposal.risk_level in HIGH_RISK_LEVELS),
        "connector_options": sorted({proposal.connector for proposal in container.proposal_service.list()}),
    }
    return _render(templates, request, "templates/proposals.html", container, **context)


@router.get("/approvals")
def approvals_page(
    request: Request,
    risk_filter: str | None = None,
    connector_filter: str | None = None,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    proposals = _filter_proposals(
        container.proposal_service.list(ProposalStatus.PENDING.value),
        None,
        risk_filter,
        connector_filter,
        None,
    )
    context = {
        "title": "Approval UI",
        "proposals": proposals,
        "risk_filter": risk_filter,
        "connector_filter": connector_filter,
        "high_risk_count": sum(1 for proposal in proposals if proposal.risk_level in HIGH_RISK_LEVELS),
        "connector_options": sorted({proposal.connector for proposal in container.proposal_service.list()}),
    }
    return _render(templates, request, "templates/approvals.html", container, **context)


@router.get("/proposals/{proposal_id}")
def proposal_detail_page(
    proposal_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    proposal = container.proposal_service.get(proposal_id)
    approvals = container.proposal_service.list_approvals(proposal_id)
    history = [entry for entry in container.history_service.list_action_history(limit=200) if entry.proposal_id == proposal_id]
    jobs = [job for job in container.execution_queue_service.list_recent(limit=200) if job.proposal_id == proposal_id]
    snapshot_context = _proposal_snapshot_context(container, proposal)
    run_context = _run_context(container, proposal.run_id)
    context = {
        "title": "Proposal Detail",
        "proposal": proposal,
        "approvals": approvals,
        "history": history,
        "jobs": jobs,
        "preview": snapshot_context["preview"],
        "snapshot_context": snapshot_context,
        "agent_runs": run_context["agent_runs"],
        "handoffs": run_context["handoffs"],
        "require_reauth_for_approval": proposal.risk_level in HIGH_RISK_LEVELS and not user.recent_auth,
    }
    return _render(templates, request, "templates/proposal_detail.html", container, **context)


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    request: Request,
    reason: str = Form(...),
    current_password: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        container.rate_limiter.check(
            f"approval:{_client_key(request)}",
            container.base_settings.approval_rate_limit_attempts,
            container.base_settings.approval_rate_limit_window_seconds,
        )
        proposal = container.proposal_service.get(proposal_id)
        if proposal.risk_level in HIGH_RISK_LEVELS:
            _require_recent_auth(request, container, user, current_password, "approve a high-risk action")
        container.runtime_service.approve_and_queue(
            proposal_id,
            ApprovalDecisionRequest(actor=user.username, reason=reason, current_password=current_password),
        )
        return _redirect(
            f"/proposals/{proposal_id}",
            message="Proposal approved and queued for the isolated worker.",
        )
    except (AuthenticationError, AuthorizationError, CsrfError, InvalidStateError, NotFoundError, RateLimitError) as exc:
        return _redirect(f"/proposals/{proposal_id}", error=str(exc))


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    request: Request,
    reason: str = Form(...),
    container: AppContainer = Depends(get_container),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        container.rate_limiter.check(
            f"approval:{_client_key(request)}",
            container.base_settings.approval_rate_limit_attempts,
            container.base_settings.approval_rate_limit_window_seconds,
        )
        container.runtime_service.reject(
            proposal_id,
            ApprovalDecisionRequest(actor=user.username, reason=reason),
        )
        return _redirect(f"/proposals/{proposal_id}", message="Proposal rejected.")
    except (AuthenticationError, AuthorizationError, CsrfError, InvalidStateError, NotFoundError, RateLimitError) as exc:
        return _redirect(f"/proposals/{proposal_id}", error=str(exc))


@router.post("/approvals/{proposal_id}/approve")
async def approve_proposal_row(
    proposal_id: str,
    request: Request,
    reason: str = Form(...),
    current_password: str | None = Form(None),
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        proposal = container.proposal_service.get(proposal_id)
        if proposal.risk_level in HIGH_RISK_LEVELS:
            return _redirect(f"/proposals/{proposal_id}", error="High-risk approvals require the detail page re-auth flow.")
        container.rate_limiter.check(
            f"approval:{_client_key(request)}",
            container.base_settings.approval_rate_limit_attempts,
            container.base_settings.approval_rate_limit_window_seconds,
        )
        container.runtime_service.approve_and_queue(
            proposal_id,
            ApprovalDecisionRequest(actor=user.username, reason=reason, current_password=current_password),
        )
        return _redirect("/approvals", message="Proposal approved and queued.")
    except (AuthenticationError, AuthorizationError, CsrfError, InvalidStateError, NotFoundError, RateLimitError) as exc:
        return _redirect("/approvals", error=str(exc))


@router.post("/approvals/{proposal_id}/reject")
async def reject_proposal_row(
    proposal_id: str,
    request: Request,
    reason: str = Form(...),
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        container.rate_limiter.check(
            f"approval:{_client_key(request)}",
            container.base_settings.approval_rate_limit_attempts,
            container.base_settings.approval_rate_limit_window_seconds,
        )
        container.runtime_service.reject(
            proposal_id,
            ApprovalDecisionRequest(actor=user.username, reason=reason),
        )
        return _redirect("/approvals", message="Proposal rejected.")
    except (AuthenticationError, AuthorizationError, CsrfError, InvalidStateError, NotFoundError, RateLimitError) as exc:
        return _redirect("/approvals", error=str(exc))


@router.get("/history")
def history_page(
    request: Request,
    agent_filter: str | None = None,
    actor_filter: str | None = None,
    connector_filter: str | None = None,
    provider_filter: str | None = None,
    risk_filter: str | None = None,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    all_history = container.history_service.list_action_history(limit=150)
    proposals = {proposal.id: proposal for proposal in container.proposal_service.list()}
    history = all_history
    if connector_filter:
        history = [entry for entry in history if entry.connector == connector_filter]
    if provider_filter:
        history = [
            entry
            for entry in history
            if proposals.get(entry.proposal_id) and proposals[entry.proposal_id].provider_name == provider_filter
        ]
    if risk_filter:
        history = [entry for entry in history if proposals.get(entry.proposal_id) and proposals[entry.proposal_id].risk_level.value == risk_filter]
    audit_rows = container.database.fetch_all(
        "SELECT * FROM audit_entries ORDER BY created_at DESC LIMIT 150"
    )
    if actor_filter:
        audit_rows = [row for row in audit_rows if actor_filter in row["payload_json"]]
    agent_runs = [
        run
        for recent in container.agent_service.recent_runs(limit=20)
        for run in container.agent_service.list_run_history(recent["run_id"])
    ]
    if agent_filter:
        agent_runs = [run for run in agent_runs if run.agent_id == agent_filter or run.role.value == agent_filter]
    connector_runs = container.history_service.list_connector_runs(limit=50)
    if agent_filter:
        connector_runs = [
            run
            for run in connector_runs
            if run.agent_id == agent_filter or run.agent_role == agent_filter
        ]
    if connector_filter:
        connector_runs = [run for run in connector_runs if run.connector == connector_filter]
    context = {
        "title": "Action History",
        "history": history,
        "connector_runs": connector_runs,
        "jobs": container.execution_queue_service.list_recent(limit=50),
        "audit_status": container.audit_service.verify_integrity(),
        "audit_entries": audit_rows,
        "agent_runs": agent_runs,
        "agent_filter": agent_filter,
        "actor_filter": actor_filter,
        "connector_filter": connector_filter,
        "provider_filter": provider_filter,
        "risk_filter": risk_filter,
        "agent_options": container.agent_service.list_agents(),
        "connector_options": sorted({proposal.connector for proposal in proposals.values()}),
        "provider_options": sorted({proposal.provider_name for proposal in proposals.values() if proposal.provider_name}),
    }
    return _render(templates, request, "templates/history.html", container, **context)


@router.get("/connectors")
def connectors_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    return _render(
        templates,
        request,
        "templates/connectors.html",
        container,
        title="Connector Status",
        connectors=container.connector_registry.list_health(),
        settings=container.settings_service.get_effective_settings(),
    )


@router.get("/settings")
def settings_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    context = _settings_page_context(container, result=None)
    context["requires_recent_auth"] = not user.recent_auth
    return _render(templates, request, "templates/settings.html", container, **context)


@router.post("/settings")
async def settings_submit(
    request: Request,
    runtime_mode: str = Form(...),
    provider: str = Form(...),
    fallback_provider: str | None = Form(None),
    model: str = Form(...),
    base_url: str | None = Form(None),
    api_key_env: str = Form(...),
    generic_http_endpoint: str | None = Form(None),
    provider_timeout_seconds: float = Form(...),
    provider_max_retries: int = Form(...),
    provider_circuit_breaker_threshold: int = Form(...),
    provider_circuit_breaker_seconds: int = Form(...),
    summary_profile: str = Form(...),
    planning_profile: str = Form(...),
    fast_provider: str | None = Form(None),
    cheap_provider: str | None = Form(None),
    strong_provider: str | None = Form(None),
    local_provider: str | None = Form(None),
    privacy_provider: str | None = Form(None),
    provider_allowed_hosts: str = Form(...),
    allow_provider_private_network: bool = Form(False),
    allow_restricted_provider_egress: bool = Form(False),
    json_audit_enabled: bool = Form(False),
    session_max_age_seconds: int = Form(...),
    session_idle_timeout_seconds: int = Form(...),
    recent_auth_window_seconds: int = Form(...),
    max_request_size_bytes: int = Form(...),
    allowed_http_schemes: str = Form(...),
    allowed_http_ports: str = Form(...),
    allow_http_private_network: bool = Form(False),
    http_follow_redirects: bool = Form(False),
    http_timeout_seconds: float = Form(...),
    http_max_response_bytes: int = Form(...),
    filesystem_max_read_bytes: int = Form(...),
    allowed_filesystem_roots: str = Form(...),
    allowed_http_hosts: str = Form(...),
    enable_system_connector: bool = Form(False),
    enable_outlook_connector: bool = Form(False),
    local_protection_mode: str = Form(...),
    history_retention_days: int = Form(...),
    cli_token_ttl_seconds: int = Form(...),
    worker_lease_seconds: int = Form(...),
    worker_max_attempts: int = Form(...),
    current_password: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        _require_recent_auth(request, container, user, current_password, "change runtime settings")
        container.settings_service.save(
            SettingsUpdate(
                runtime_mode=runtime_mode,
                provider=provider,
                fallback_provider=fallback_provider or None,
                model=model,
                base_url=base_url or None,
                api_key_env=api_key_env,
                generic_http_endpoint=generic_http_endpoint or None,
                provider_timeout_seconds=provider_timeout_seconds,
                provider_max_retries=provider_max_retries,
                provider_circuit_breaker_threshold=provider_circuit_breaker_threshold,
                provider_circuit_breaker_seconds=provider_circuit_breaker_seconds,
                summary_profile=summary_profile,
                planning_profile=planning_profile,
                fast_provider=fast_provider or None,
                cheap_provider=cheap_provider or None,
                strong_provider=strong_provider or None,
                local_provider=local_provider or None,
                privacy_provider=privacy_provider or None,
                provider_allowed_hosts=provider_allowed_hosts,
                allow_provider_private_network=allow_provider_private_network,
                allow_restricted_provider_egress=allow_restricted_provider_egress,
                json_audit_enabled=json_audit_enabled,
                session_max_age_seconds=session_max_age_seconds,
                session_idle_timeout_seconds=session_idle_timeout_seconds,
                recent_auth_window_seconds=recent_auth_window_seconds,
                max_request_size_bytes=max_request_size_bytes,
                allowed_http_schemes=allowed_http_schemes,
                allowed_http_ports=allowed_http_ports,
                allow_http_private_network=allow_http_private_network,
                http_follow_redirects=http_follow_redirects,
                http_timeout_seconds=http_timeout_seconds,
                http_max_response_bytes=http_max_response_bytes,
                filesystem_max_read_bytes=filesystem_max_read_bytes,
                allowed_filesystem_roots=allowed_filesystem_roots,
                allowed_http_hosts=allowed_http_hosts,
                enable_system_connector=enable_system_connector,
                enable_outlook_connector=enable_outlook_connector,
                local_protection_mode=local_protection_mode,
                history_retention_days=history_retention_days,
                cli_token_ttl_seconds=cli_token_ttl_seconds,
                worker_lease_seconds=worker_lease_seconds,
                worker_max_attempts=worker_max_attempts,
            ),
            actor=user.username,
            reason="settings-update",
        )
        container.audit_service.emit(
            "settings.updated",
            {
                "actor": user.username,
                "runtime_mode": runtime_mode,
                "provider": provider,
                "enable_system_connector": enable_system_connector,
                "enable_outlook_connector": enable_outlook_connector,
                "allow_provider_private_network": allow_provider_private_network,
                "allow_restricted_provider_egress": allow_restricted_provider_egress,
                "local_protection_mode": local_protection_mode,
                "cli_token_ttl_seconds": cli_token_ttl_seconds,
            },
        )
        return _redirect("/settings", message="Settings saved.")
    except (AuthenticationError, AuthorizationError, CsrfError) as exc:
        return _redirect("/settings", error=str(exc))


@router.post("/settings/reset")
async def settings_reset(
    request: Request,
    current_password: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        _require_recent_auth(request, container, user, current_password, "reset runtime settings")
        container.settings_service.reset_to_safe_defaults(actor=user.username, reason="reset-safe-defaults")
        container.audit_service.emit("settings.reset_to_safe_defaults", {"actor": user.username})
        return _redirect("/settings", message="Safe defaults restored.")
    except (AuthenticationError, AuthorizationError, CsrfError) as exc:
        return _redirect("/settings", error=str(exc))


@router.get("/settings/export")
def settings_export(
    request: Request,
    container: AppContainer = Depends(get_container),
):
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        return user
    exported = container.settings_service.export_sanitized()
    return JSONResponse(content=exported.model_dump(mode="json"))


@router.post("/settings/import")
async def settings_import(
    request: Request,
    settings_json: str = Form(...),
    current_password: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    try:
        await validate_csrf(request)
        user = _page_user_or_redirect(request, container)
        if isinstance(user, RedirectResponse):
            return user
        _require_recent_auth(request, container, user, current_password, "import runtime settings")
        exported = SanitizedSettingsExport(**json.loads(settings_json))
        container.settings_service.import_sanitized(exported, actor=user.username, reason="import-sanitized-settings")
        container.audit_service.emit("settings.imported", {"actor": user.username})
        return _redirect("/settings", message="Sanitized settings imported.")
    except (AuthenticationError, AuthorizationError, CsrfError, ValueError, json.JSONDecodeError) as exc:
        return _redirect("/settings", error=str(exc))


@router.post("/settings/test-provider")
async def test_provider_partial(
    request: Request,
    provider_name: str = Form(...),
    model_name: str = Form(...),
    prompt: str = Form("Return a one-line readiness confirmation."),
    data_classification: str = Form("external-ok"),
    base_url: str | None = Form(None),
    api_key_env: str | None = Form(None),
    generic_http_endpoint: str | None = Form(None),
    provider_timeout_seconds: float | None = Form(None),
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    await validate_csrf(request)
    user = _page_user_or_redirect(request, container)
    if isinstance(user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Authentication required.")
    settings_override = _temporary_settings(
        container,
        provider_name=provider_name,
        model_name=model_name,
        base_url=base_url,
        api_key_env=api_key_env,
        generic_http_endpoint=generic_http_endpoint,
        provider_timeout_seconds=provider_timeout_seconds,
    )
    result = container.provider_service.test_provider(
        ProviderTestRequest(
            provider_name=provider_name,
            model_name=model_name,
            prompt=prompt,
            data_classification=data_classification,
        ),
        settings_override=settings_override,
    )
    context = _settings_page_context(container, result=result)
    context["requires_recent_auth"] = not user.recent_auth
    return _render(templates, request, "templates/settings.html", container, **context)


@router.get("/api/proposals")
def api_list_proposals(
    request: Request,
    status_filter: str | None = None,
    container: AppContainer = Depends(get_container),
):
    _api_user_or_401(request, container)
    return container.proposal_service.list(status_filter)


@router.get("/api/proposals/{proposal_id}")
def api_get_proposal(
    proposal_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    _api_user_or_401(request, container)
    try:
        return container.proposal_service.get(proposal_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/runs")
async def api_run_agent(
    request: Request,
    payload: AgentRunRequest,
    container: AppContainer = Depends(get_container),
):
    _api_user_or_401(request, container)
    await validate_csrf(request)
    return container.runtime_service.run_agent(payload)


@router.get("/api/runs/{run_id}")
def api_get_run(
    run_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    _api_user_or_401(request, container)
    context = _run_context(container, run_id)
    if context["summary"] is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} was not found.")
    return context


@router.post("/api/proposals/{proposal_id}/approve")
async def api_approve_proposal(
    proposal_id: str,
    request: Request,
    payload: ApprovalDecisionRequest,
    container: AppContainer = Depends(get_container),
):
    user = _api_user_or_401(request, container)
    await validate_csrf(request)
    container.rate_limiter.check(
        f"approval:{_client_key(request)}",
        container.base_settings.approval_rate_limit_attempts,
        container.base_settings.approval_rate_limit_window_seconds,
    )
    proposal = container.proposal_service.get(proposal_id)
    if proposal.risk_level in HIGH_RISK_LEVELS:
        _require_recent_auth(
            request,
            container,
            user,
            payload.current_password,
            "approve a high-risk action",
        )
    try:
        result = container.runtime_service.approve_and_queue(
            proposal_id,
            payload.model_copy(update={"actor": user.username}),
        )
        return JSONResponse(content=jsonable_encoder(result), status_code=202)
    except (InvalidStateError, NotFoundError, AuthorizationError, AuthenticationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/proposals/{proposal_id}/reject")
async def api_reject_proposal(
    proposal_id: str,
    request: Request,
    payload: ApprovalDecisionRequest,
    container: AppContainer = Depends(get_container),
):
    user = _api_user_or_401(request, container)
    await validate_csrf(request)
    container.rate_limiter.check(
        f"approval:{_client_key(request)}",
        container.base_settings.approval_rate_limit_attempts,
        container.base_settings.approval_rate_limit_window_seconds,
    )
    try:
        return container.runtime_service.reject(
            proposal_id,
            payload.model_copy(update={"actor": user.username}),
        )
    except (InvalidStateError, NotFoundError, RateLimitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/history")
def api_history(request: Request, container: AppContainer = Depends(get_container)):
    _api_user_or_401(request, container)
    return {
        "actions": container.history_service.list_action_history(limit=100),
        "connector_runs": container.history_service.list_connector_runs(limit=100),
        "jobs": container.execution_queue_service.list_recent(limit=100),
        "agent_runs": [
            run
            for recent in container.agent_service.recent_runs(limit=20)
            for run in container.agent_service.list_run_history(recent["run_id"])
        ],
    }


@router.get("/api/providers")
def api_providers(request: Request, container: AppContainer = Depends(get_container)):
    _api_user_or_401(request, container)
    return container.provider_service.list_statuses()


@router.get("/api/agents")
def api_agents(request: Request, container: AppContainer = Depends(get_container)):
    _api_user_or_401(request, container)
    return container.agent_service.list_agents()


@router.get("/api/connectors")
def api_connectors(request: Request, container: AppContainer = Depends(get_container)):
    _api_user_or_401(request, container)
    return container.connector_registry.list_health()


@router.get("/api/jobs")
def api_jobs(request: Request, container: AppContainer = Depends(get_container)):
    _api_user_or_401(request, container)
    return container.execution_queue_service.list_recent(limit=100)


@router.get("/api/audit/verify")
def api_verify_audit(request: Request, container: AppContainer = Depends(get_container)):
    _api_user_or_401(request, container)
    return container.audit_service.verify_integrity()


@router.post("/api/providers/test")
async def api_test_provider(
    request: Request,
    payload: ProviderTestRequest,
    container: AppContainer = Depends(get_container),
):
    _api_user_or_401(request, container)
    await validate_csrf(request)
    result = container.provider_service.test_provider(payload)
    status_code = 200 if result.ok else 400
    return JSONResponse(content=result.model_dump(), status_code=status_code)
