from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.dependencies import get_container, get_templates
from app.core.container import AppContainer
from app.core.errors import InvalidStateError, NotFoundError
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest, ProposalStatus
from app.schemas.providers import ProviderTestRequest
from app.schemas.settings import SettingsUpdate


router = APIRouter()


@router.get("/")
def dashboard(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    proposals = container.proposal_service.list()
    counts = Counter(proposal.status.value for proposal in proposals)
    recent_history = container.history_service.list_action_history(limit=10)
    recent_summaries = container.summary_service.list_recent(limit=5)
    context = {
        "request": request,
        "counts": counts,
        "recent_history": recent_history,
        "recent_summaries": recent_summaries,
        "providers": container.provider_service.list_statuses(),
        "connectors": container.connector_registry.list_health(),
        "message": request.query_params.get("message"),
    }
    return templates.TemplateResponse(request=request, name="templates/dashboard.html", context=context)


@router.get("/run")
def run_agent_form(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    context = {
        "request": request,
        "providers": container.provider_service.list_statuses(),
        "settings": container.settings_service.get_effective_settings(),
        "message": request.query_params.get("message"),
    }
    return templates.TemplateResponse(request=request, name="templates/run_agent.html", context=context)


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
    powershell_command: str | None = Form(None),
    provider_name: str | None = Form(None),
    model_name: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
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
        powershell_command=powershell_command or None,
        provider_name=provider_name or None,
        model_name=model_name or None,
    )
    result = container.runtime_service.run_agent(run_request)
    return RedirectResponse(
        url=f"/proposals?run_id={result.run_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/proposals")
def proposals_page(
    request: Request,
    status_filter: str | None = None,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    proposals = container.proposal_service.list(status_filter)
    run_id = request.query_params.get("run_id")
    if run_id:
        proposals = [proposal for proposal in proposals if proposal.run_id == run_id]
    context = {
        "request": request,
        "proposals": proposals,
        "status_filter": status_filter,
        "message": request.query_params.get("message"),
    }
    return templates.TemplateResponse(request=request, name="templates/proposals.html", context=context)


@router.get("/approvals")
def approvals_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    proposals = container.proposal_service.list(ProposalStatus.PENDING.value)
    context = {"request": request, "proposals": proposals, "message": request.query_params.get("message")}
    return templates.TemplateResponse(request=request, name="templates/approvals.html", context=context)


@router.get("/proposals/{proposal_id}")
def proposal_detail_page(
    proposal_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    proposal = container.proposal_service.get(proposal_id)
    approvals = container.proposal_service.list_approvals(proposal_id)
    history = [entry for entry in container.history_service.list_action_history(limit=100) if entry.proposal_id == proposal_id]
    context = {
        "request": request,
        "proposal": proposal,
        "approvals": approvals,
        "history": history,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }
    return templates.TemplateResponse(request=request, name="templates/proposal_detail.html", context=context)


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: str,
    actor: str = Form("operator"),
    reason: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    try:
        container.runtime_service.approve_and_execute(
            proposal_id,
            ApprovalDecisionRequest(actor=actor, reason=reason or None),
        )
        return RedirectResponse(
            url=f"/proposals/{proposal_id}?message=Proposal approved and executed.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/proposals/{proposal_id}?error={str(exc)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: str,
    actor: str = Form("operator"),
    reason: str | None = Form(None),
    container: AppContainer = Depends(get_container),
):
    container.runtime_service.reject(
        proposal_id,
        ApprovalDecisionRequest(actor=actor, reason=reason or None),
    )
    return RedirectResponse(
        url=f"/proposals/{proposal_id}?message=Proposal rejected.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/approvals/{proposal_id}/approve")
def approve_proposal_row(
    proposal_id: str,
    request: Request,
    actor: str = Form("operator"),
    reason: str | None = Form(None),
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    try:
        container.runtime_service.approve_and_execute(
            proposal_id,
            ApprovalDecisionRequest(actor=actor, reason=reason or None),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    proposal = container.proposal_service.get(proposal_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/proposal_row.html",
        context={"request": request, "proposal": proposal, "compact": True},
    )


@router.post("/approvals/{proposal_id}/reject")
def reject_proposal_row(
    proposal_id: str,
    request: Request,
    actor: str = Form("operator"),
    reason: str | None = Form(None),
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    container.runtime_service.reject(
        proposal_id,
        ApprovalDecisionRequest(actor=actor, reason=reason or None),
    )
    proposal = container.proposal_service.get(proposal_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/proposal_row.html",
        context={"request": request, "proposal": proposal, "compact": True},
    )


@router.get("/history")
def history_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    context = {
        "request": request,
        "history": container.history_service.list_action_history(limit=100),
        "connector_runs": container.history_service.list_connector_runs(limit=50),
    }
    return templates.TemplateResponse(request=request, name="templates/history.html", context=context)


@router.get("/connectors")
def connectors_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    return templates.TemplateResponse(
        request=request,
        name="templates/connectors.html",
        context={"request": request, "connectors": container.connector_registry.list_health()},
    )


@router.get("/settings")
def settings_page(
    request: Request,
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    context = {
        "request": request,
        "settings": container.settings_service.get_effective_settings(),
        "providers": container.provider_service.list_statuses(),
        "message": request.query_params.get("message"),
    }
    return templates.TemplateResponse(request=request, name="templates/settings.html", context=context)


@router.post("/settings")
def settings_submit(
    runtime_mode: str = Form(...),
    provider: str = Form(...),
    fallback_provider: str | None = Form(None),
    model: str = Form(...),
    base_url: str | None = Form(None),
    api_key_env: str = Form(...),
    generic_http_endpoint: str | None = Form(None),
    provider_timeout_seconds: float = Form(...),
    provider_max_retries: int = Form(...),
    json_audit_enabled: bool = Form(False),
    allowed_filesystem_roots: str = Form(...),
    allowed_http_hosts: str = Form(...),
    powershell_allowlist: str = Form(...),
    container: AppContainer = Depends(get_container),
):
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
            json_audit_enabled=json_audit_enabled,
            allowed_filesystem_roots=allowed_filesystem_roots,
            allowed_http_hosts=allowed_http_hosts,
            powershell_allowlist=powershell_allowlist,
        )
    )
    return RedirectResponse(
        url="/settings?message=Settings saved.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/settings/test-provider")
def test_provider_partial(
    request: Request,
    provider_name: str = Form(...),
    model_name: str = Form(...),
    prompt: str = Form("Return a one-line readiness confirmation."),
    container: AppContainer = Depends(get_container),
    templates: Jinja2Templates = Depends(get_templates),
):
    result = container.provider_service.test_provider(
        ProviderTestRequest(provider_name=provider_name, model_name=model_name, prompt=prompt)
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/provider_test_result.html",
        context={"request": request, "result": result},
    )


@router.post("/api/runs")
def api_run_agent(payload: AgentRunRequest, container: AppContainer = Depends(get_container)):
    return container.runtime_service.run_agent(payload)


@router.get("/api/proposals")
def api_list_proposals(
    status_filter: str | None = None,
    container: AppContainer = Depends(get_container),
):
    return container.proposal_service.list(status_filter)


@router.get("/api/proposals/{proposal_id}")
def api_get_proposal(proposal_id: str, container: AppContainer = Depends(get_container)):
    try:
        return container.proposal_service.get(proposal_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/proposals/{proposal_id}/approve")
def api_approve_proposal(
    proposal_id: str,
    payload: ApprovalDecisionRequest,
    container: AppContainer = Depends(get_container),
):
    try:
        return container.runtime_service.approve_and_execute(proposal_id, payload)
    except (InvalidStateError, NotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/proposals/{proposal_id}/reject")
def api_reject_proposal(
    proposal_id: str,
    payload: ApprovalDecisionRequest,
    container: AppContainer = Depends(get_container),
):
    try:
        return container.runtime_service.reject(proposal_id, payload)
    except (InvalidStateError, NotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/history")
def api_history(container: AppContainer = Depends(get_container)):
    return {
        "actions": container.history_service.list_action_history(limit=100),
        "connector_runs": container.history_service.list_connector_runs(limit=100),
    }


@router.get("/api/providers")
def api_providers(container: AppContainer = Depends(get_container)):
    return container.provider_service.list_statuses()


@router.get("/api/connectors")
def api_connectors(container: AppContainer = Depends(get_container)):
    return container.connector_registry.list_health()


@router.post("/api/providers/test")
def api_test_provider(payload: ProviderTestRequest, container: AppContainer = Depends(get_container)):
    result = container.provider_service.test_provider(payload)
    status_code = 200 if result.ok else 400
    return JSONResponse(content=result.model_dump(), status_code=status_code)
