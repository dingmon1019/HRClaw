from __future__ import annotations

import argparse
import json

from app.core.container import AppContainer
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
    run_agent.add_argument("--command", dest="powershell_command")
    run_agent.add_argument("--provider", dest="provider_name")
    run_agent.add_argument("--model", dest="model_name")

    list_proposals = subparsers.add_parser("list-proposals", help="List proposals.")
    list_proposals.add_argument("--status")

    approve = subparsers.add_parser("approve-proposal", help="Approve and execute a proposal.")
    approve.add_argument("proposal_id")
    approve.add_argument("--actor", default="cli-operator")
    approve.add_argument("--reason")

    reject = subparsers.add_parser("reject-proposal", help="Reject a proposal.")
    reject.add_argument("proposal_id")
    reject.add_argument("--actor", default="cli-operator")
    reject.add_argument("--reason")

    show_history = subparsers.add_parser("show-history", help="Show action history.")
    show_history.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("list-providers", help="List provider status.")

    test_provider = subparsers.add_parser("test-provider", help="Run a provider smoke test.")
    test_provider.add_argument("--provider", dest="provider_name")
    test_provider.add_argument("--model", dest="model_name")
    test_provider.add_argument(
        "--prompt",
        default="Return a one-line readiness confirmation.",
    )
    return parser


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
                powershell_command=args.powershell_command,
                provider_name=args.provider_name,
                model_name=args.model_name,
            )
        )
        print(f"Run: {result.run_id}")
        print(f"Summary: {result.summary.summary_text}")
        print("Proposals:")
        for proposal in result.proposals:
            print(f"  - {proposal.id} | {proposal.status.value} | {proposal.title}")
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
        result = container.runtime_service.approve_and_execute(
            args.proposal_id,
            ApprovalDecisionRequest(actor=args.actor, reason=args.reason),
        )
        print(json.dumps(result["result"], indent=2, default=str))
        return

    if args.command == "reject-proposal":
        result = container.runtime_service.reject(
            args.proposal_id,
            ApprovalDecisionRequest(actor=args.actor, reason=args.reason),
        )
        print(f"{result.proposal_id} rejected by {result.actor} at {result.created_at}")
        return

    if args.command == "show-history":
        for entry in container.history_service.list_action_history(limit=args.limit):
            print(f"{entry.started_at} | {entry.status} | {entry.action_type} | {entry.proposal_id}")
        return

    if args.command == "list-providers":
        for provider in container.provider_service.list_statuses():
            print(f"{provider.name} | configured={provider.configured} | {provider.description}")
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


if __name__ == "__main__":
    main()

