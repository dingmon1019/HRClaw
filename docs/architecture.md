# Architecture

## Identity

Win Agent Runtime is a Windows-first localhost multi-agent runtime.

It is inspired by NemoClaw-style approval safety and OpenClaw-style provider flexibility.

It is not an official implementation of either project.
It is not OpenShell-level sandboxing.

## Main Surfaces

The product has two integrated experiences:

1. Assistant Workbench
   Natural language goal entry, task decomposition, agent collaboration view, proposals, risks, and progress.
2. Operator Control Console
   Dashboard, approvals inbox, proposal detail, history, audit verification, connectors, and settings.

## Runtime Flow

The current runtime uses an explicit role handoff sequence:

1. Supervisor Agent
   Interprets the objective and decomposes it into subtasks.
2. Planner Agent
   Collects bounded context and creates typed candidate actions.
3. Reviewer Agent
   Applies policy, risk, workspace, and provider egress review.
4. Operator
   Approves or rejects specific proposals.
5. Executor Agent
   Runs approved snapshots only through the worker.
6. Reporter Agent
   Summarizes the plan and later surfaces outcome context.

## Core Modules

- `app/agents`
  Agent registry, agent runs, and handoff persistence.
- `app/runtime`
  Planning, runtime service, execution dispatch, and worker.
- `app/policy`
  Filesystem guard, HTTP/provider egress validation, and risk policy.
- `app/providers`
  Provider adapters and provider HTTP client.
- `app/connectors`
  Bounded local connectors.
- `app/audit`
  Append-oriented tamper-evident audit trail.
- `app/services`
  Proposal snapshots, settings versioning, queue management, history, auth, and provider orchestration.

## Persistence

SQLite tables now support both control-plane state and multi-agent visibility:

- `agents`
- `agent_runs`
- `handoffs`
- `proposals`
- `proposal_snapshots`
- `approvals`
- `summaries`
- `execution_jobs`
- `execution_attempts`
- `action_history`
- `connector_runs`
- `audit_entries`
- `provider_health`
- `connector_health`
- `settings`
- `settings_versions`
- `users`
- `tasks`

## Execution Boundary

The web process does not execute dangerous actions directly.

Instead:

1. approved proposals are queued in `execution_jobs`
2. the worker claims a job with a lease
3. the executor re-verifies the approved snapshot
4. the bounded connector action runs
5. history, attempts, and audit are updated

This is safer than direct in-request execution, but it is still not a host isolation boundary.

## Provider Boundary

Provider traffic is part of the security model.

Provider adapters share:

- host allowlists
- scheme and port restrictions
- private-network controls
- restricted-data refusal by default
- routing profiles
- retries and circuit breaking

## Windows Focus

The repository stays Windows-friendly by design:

- FastAPI + Jinja2 UI
- SQLite
- PowerShell bootstrap/run scripts
- optional Outlook connector through `pywin32`
- no Docker requirement
- no WSL requirement
