# Architecture

## Identity

Win Agent Runtime is a Windows-first local agent runtime inspired by NemoClaw-style approval safety and by multi-provider ideas similar to OpenClaw. It is not an official implementation of either project.

## Flow

1. Collect context through read-safe connectors.
2. Summarize the context with the selected provider or a local fallback.
3. Generate structured action proposals.
4. Persist proposals in SQLite.
5. Require operator approval before execution.
6. Execute through a connector dispatcher.
7. Audit the approval, execution, and outcome.

## Safety Model

- Policy engine evaluates every proposal.
- Allowlists constrain filesystem roots, HTTP hosts, and PowerShell commands.
- Side-effecting actions are marked and risk-scored.
- Pending proposals are durable in SQLite.
- Executions are logged to `action_history`.
- Approval and rejection decisions are stored separately in `approvals`.
- JSON audit logging is optional and can be disabled.

## Layers

- `app/runtime`: planning loop and execution dispatcher
- `app/policy`: allowlist enforcement and approval logic
- `app/providers`: multi-provider abstraction and adapters
- `app/connectors`: modular local and network connectors
- `app/api`: FastAPI routes and localhost UI
- `app/memory`: summary persistence
- `app/audit`: JSON audit stream

