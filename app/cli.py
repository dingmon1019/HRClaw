from __future__ import annotations

import argparse
from getpass import getpass
import json
from pathlib import Path
from time import sleep

from app.core.container import AppContainer
from app.core.errors import AuthorizationError
from app.schemas.actions import ApprovalDecisionRequest, AgentRunRequest
from app.schemas.providers import ProviderTestRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Win Agent Runtime CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_agent = subparsers.add_parser("run-agent", help="Collect, summarize, and propose actions.")
    run_agent.add_argument("objective")
    run_agent.add_argument("--path", dest="filesystem_path")
    run_agent.add_argument("--file-content")
    run_agent.add_argument("--url", dest="http_url")
    run_agent.add_argument("--method", dest="http_method", default="GET")
    run_agent.add_argument("--body", dest="http_body")
    run_agent.add_argument("--headers", dest="http_headers_text")
    run_agent.add_argument("--task-title")
    run_agent.add_argument("--task-details")
    run_agent.add_argument("--system-action")
    run_agent.add_argument("--system-path")
    run_agent.add_argument("--provider", dest="provider_name")
    run_agent.add_argument("--model", dest="model_name")

    list_proposals = subparsers.add_parser("list-proposals", help="List proposals.")
    list_proposals.add_argument("--status")

    approve = subparsers.add_parser("approve-proposal", help="Approve and queue a proposal.")
    approve.add_argument("proposal_id")
    approve.add_argument("--reason", required=True)
    approve.add_argument("--username")
    approve.add_argument("--token-file")

    reject = subparsers.add_parser("reject-proposal", help="Reject a proposal.")
    reject.add_argument("proposal_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--username")
    reject.add_argument("--token-file")

    worker = subparsers.add_parser("run-worker", help="Run the constrained execution worker.")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--limit", type=int, default=1)
    worker.add_argument("--interval", type=float, default=2.0)
    worker.add_argument("--username")
    worker.add_argument("--token-file")

    jobs = subparsers.add_parser("list-jobs", help="List recent execution jobs.")
    jobs.add_argument("--limit", type=int, default=20)

    show_history = subparsers.add_parser("show-history", help="Show action history.")
    show_history.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("list-providers", help="List provider status.")

    test_provider = subparsers.add_parser("test-provider", help="Run a provider smoke test.")
    test_provider.add_argument("--provider", dest="provider_name")
    test_provider.add_argument("--model", dest="model_name")
    test_provider.add_argument("--prompt", default="Return a one-line readiness confirmation.")

    issue_cli = subparsers.add_parser("issue-cli-token", help="Issue a short-lived CLI auth token.")
    issue_cli.add_argument("--username", required=True)
    issue_cli.add_argument("--purpose", default="worker")
    issue_cli.add_argument("--ttl-seconds", type=int, default=None)
    issue_cli.add_argument("--token-file")
    issue_cli.add_argument("--emit-token", action="store_true")

    set_credential = subparsers.add_parser("set-credential", help="Store or update a Windows Credential Manager secret.")
    set_credential.add_argument("--target", required=True)
    set_credential.add_argument("--username", default=None)

    delete_credential = subparsers.add_parser("delete-credential", help="Delete a Windows Credential Manager secret.")
    delete_credential.add_argument("--target", required=True)

    test_credential = subparsers.add_parser("test-credential", help="Check whether a Windows Credential Manager target exists.")
    test_credential.add_argument("--target", required=True)

    subparsers.add_parser("verify-audit", help="Verify tamper-evident audit chain integrity.")
    return parser


def _read_password(args) -> str:
    password = getpass("Password: ")
    if not password:
        raise AuthorizationError("Password input was empty.")
    return password


def _resolve_token_file(container: AppContainer, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return (container.base_settings.resolved_secrets_dir / "cli_tokens" / candidate).resolve()


def _issue_cli_token(
    container: AppContainer,
    *,
    username: str,
    password: str,
    purpose: str,
    ttl_seconds: int | None = None,
):
    return container.admin_token_service.issue(
        username=username,
        password=password,
        purpose=purpose,
        ttl_seconds=ttl_seconds,
    )


def _resolve_cli_token(container: AppContainer, args, purpose: str):
    token: str | None = None
    if getattr(args, "token_file", None):
        token = container.protected_storage.read_secret_text(
            _resolve_token_file(container, args.token_file),
            purpose="cli-token",
        ).strip()
    elif getattr(args, "username", None):
        password = _read_password(args)
        token, _ = _issue_cli_token(
            container,
            username=args.username,
            password=password,
            purpose=purpose,
        )
    else:
        raise AuthorizationError(
            "Sensitive CLI commands require either --token-file or --username with interactive password authentication."
        )
    return container.admin_token_service.verify(token, purpose=purpose)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    container = AppContainer()

    if args.command == "run-agent":
        result = container.runtime_service.run_agent(
            AgentRunRequest(
                objective=args.objective,
                filesystem_path=args.filesystem_path,
                file_content=args.file_content,
                http_url=args.http_url,
                http_method=args.http_method,
                http_body=args.http_body,
                http_headers_text=args.http_headers_text,
                task_title=args.task_title,
                task_details=args.task_details,
                system_action=args.system_action,
                system_path=args.system_path,
                provider_name=args.provider_name,
                model_name=args.model_name,
            )
        )
        print(f"Run: {result.run_id}")
        print(f"Summary: {result.summary.summary_text}")
        print("Proposals:")
        for proposal in result.proposals:
            print(f"  - {proposal.id} | {proposal.status.value} | {proposal.risk_level.value} | {proposal.title}")
        return

    if args.command == "list-proposals":
        proposals = container.proposal_service.list(args.status)
        for proposal in proposals:
            print(
                f"{proposal.id} | {proposal.status.value} | {proposal.risk_level.value} | "
                f"{proposal.connector} | {proposal.title}"
            )
        return

    if args.command == "approve-proposal":
        token_record = _resolve_cli_token(container, args, "approval")
        result = container.runtime_service.approve_and_queue(
            args.proposal_id,
            ApprovalDecisionRequest(actor=token_record.username, reason=args.reason),
        )
        print(json.dumps(result["job"].model_dump(mode="json"), indent=2))
        return

    if args.command == "reject-proposal":
        token_record = _resolve_cli_token(container, args, "approval")
        result = container.runtime_service.reject(
            args.proposal_id,
            ApprovalDecisionRequest(actor=token_record.username, reason=args.reason),
        )
        print(f"{result.proposal_id} rejected by {result.actor} at {result.created_at}")
        return

    if args.command == "run-worker":
        _resolve_cli_token(container, args, "worker")
        if args.once:
            result = container.worker.run_once()
            print(json.dumps(result, indent=2, default=str) if result is not None else "No queued jobs.")
            return

        processed = 0
        while args.limit <= 0 or processed < args.limit:
            result = container.worker.run_once()
            if result is not None:
                processed += 1
                print(json.dumps(result, indent=2, default=str))
            sleep(args.interval)
        return

    if args.command == "issue-cli-token":
        if not args.emit_token and not args.token_file:
            raise SystemExit("Use --token-file for protected storage or --emit-token for explicit advanced use.")
        password = _read_password(args)
        token, record = _issue_cli_token(
            container,
            username=args.username,
            password=password,
            purpose=args.purpose,
            ttl_seconds=args.ttl_seconds,
        )
        response = {
            "purpose": record.purpose,
            "expires_at": record.expires_at,
            "username": record.username,
        }
        if args.token_file:
            token_path = _resolve_token_file(container, args.token_file)
            container.protected_storage.write_secret_text(token_path, token, purpose="cli-token")
            response["token_file"] = str(token_path)
            response["storage_mode"] = container.protected_storage.storage_mode
            response["storage_posture"] = container.protected_storage.posture_label
        if args.emit_token:
            response["token"] = token
        print(json.dumps(response, indent=2))
        return

    if args.command == "set-credential":
        secret = getpass("Credential secret: ")
        if not secret:
            raise SystemExit("Credential secret input was empty.")
        container.windows_credential_store.write_secret(args.target, secret, username=args.username)
        print(json.dumps(container.windows_credential_store.describe(args.target), indent=2))
        return

    if args.command == "delete-credential":
        container.windows_credential_store.delete_secret(args.target)
        print(json.dumps({"target": args.target, "deleted": True}, indent=2))
        return

    if args.command == "test-credential":
        print(json.dumps(container.windows_credential_store.describe(args.target), indent=2))
        return

    if args.command == "list-jobs":
        for job in container.execution_queue_service.list_recent(limit=args.limit):
            print(f"{job.queued_at} | {job.status.value} | {job.proposal_id} | {job.id}")
        return

    if args.command == "show-history":
        for entry in container.history_service.list_action_history(limit=args.limit):
            print(f"{entry.started_at} | {entry.status} | {entry.action_type} | {entry.proposal_id}")
        return

    if args.command == "list-providers":
        for provider in container.provider_service.list_statuses():
            print(
                f"{provider.name} | configured={provider.configured} | "
                f"circuit_open={provider.circuit_open} | profiles={','.join(provider.profiles)}"
            )
        return

    if args.command == "test-provider":
        result = container.provider_service.test_provider(
            ProviderTestRequest(
                provider_name=args.provider_name,
                model_name=args.model_name,
                prompt=args.prompt,
            )
        )
        print(json.dumps(result.model_dump(), indent=2))
        return

    if args.command == "verify-audit":
        print(json.dumps(container.audit_service.verify_integrity(), indent=2))


if __name__ == "__main__":
    main()
