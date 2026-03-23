# win-agent-runtime

`win-agent-runtime` is a Windows-first local multi-agent runtime and control console inspired by NemoClaw-style approval safety and OpenClaw-style provider flexibility.

It is not an official NemoClaw implementation.
It is not an official OpenClaw implementation.
It is not OpenShell-level kernel sandboxing.

The project is designed to be safer and more usable for Windows localhost operator workflows:

- assistant-style natural language workbench
- operator control console for approvals and audit
- proposal-first execution with approval snapshot binding
- separate worker execution boundary
- bounded local connectors only, no raw shell execution
- multi-provider routing with egress controls
- SQLite-backed persistence, audit, task graph history, and clean release packaging

## Why This Exists

Many local agent stacks either optimize for raw autonomy or assume Linux-first tooling. This project takes the opposite approach:

- Windows 10/11 should be a first-class host
- localhost apps still need authentication, CSRF protection, and auditability
- approvals only matter if they are bound to what actually runs
- provider routing is part of the egress threat model
- an assistant UX should not force the operator to think in connector schemas first

## What This Project Is

This repository provides two integrated surfaces:

1. Assistant Workbench
   The operator gives a goal in natural language. The runtime decomposes it, shows agent collaboration, creates typed proposals, surfaces risks, and waits for approval.
2. Operator Control Console
   The operator reviews proposals, inspects previews and snapshot hashes, approves or rejects work, checks audit integrity, watches worker execution, and validates settings.

The runtime follows this high-level flow:

1. collect bounded local context
2. supervisor decomposes the objective
3. planner creates typed candidate actions
4. reviewer applies policy and egress checks
5. operator approves or rejects explicit proposals
6. approved snapshots are queued
7. executor worker runs the approved snapshot only
8. reporter and audit logs explain what happened

## What This Project Is Not

This repository is not:

- a kernel sandbox
- a container or VM isolation layer
- a privilege boundary comparable to OpenShell-like systems
- a remote multi-tenant service
- a blind autonomous execution runtime
- a generic shell wrapper

If you need strong host isolation, you still need OS-level sandboxing or virtualization outside this project.

## Architecture Overview

Major layers:

- `app/api`
  FastAPI routes for setup, login, workbench, approvals, settings, history, and JSON APIs.
- `app/runtime`
  Planning, execution dispatch, runtime service, and worker logic.
- `app/agents`
  Real multi-agent registry and persistence for supervisor, planner, reviewer, executor, and reporter roles.
- `app/policy`
  Filesystem path guard, HTTP/provider egress policy, and runtime approval rules.
- `app/providers`
  Multi-provider abstraction with fallback and circuit breaker behavior.
- `app/connectors`
  Filesystem, HTTP, task, bounded system actions, and optional Outlook integration.
- `app/audit`
  Hash-chained append-oriented audit trail with verification.
- `ui`
  Localhost operator UI using Jinja2, local static assets, and minimal JavaScript.

More detail:

- [architecture](D:/User_Data/Documents/Playground/win-agent-runtime/docs/architecture.md)
- [multi-agent model](D:/User_Data/Documents/Playground/win-agent-runtime/docs/multi_agent.md)
- [provider model](D:/User_Data/Documents/Playground/win-agent-runtime/docs/provider_model.md)
- [security model](D:/User_Data/Documents/Playground/win-agent-runtime/docs/security_model.md)
- [threat model](D:/User_Data/Documents/Playground/win-agent-runtime/docs/threat_model.md)
- [Windows setup](D:/User_Data/Documents/Playground/win-agent-runtime/docs/windows_setup.md)
- [release hygiene](D:/User_Data/Documents/Playground/win-agent-runtime/docs/release_hygiene.md)

## Multi-Agent Model

This project now implements explicit agent identities instead of cosmetic naming.

Included roles:

- Supervisor Agent
  Decomposes the objective and creates handoffs.
- Planner Agent
  Creates typed candidate actions and structured previews.
- Reviewer Agent
  Applies policy checks, risk review, and provider egress constraints.
- Executor Agent
  Runs approved snapshots only through the worker boundary.
- Reporter Agent
  Produces operator-facing explanations of the plan and outcome.

Real multi-agent properties in the codebase:

- agent registry with role definitions
- per-agent provider profiles
- enforced per-agent capability and connector restrictions
- agent run history in SQLite
- explicit handoff records in SQLite
- persisted task nodes with parent/dependency links
- visible task graph/timeline in the UI
- proposal provenance linked back to planner/reviewer roles

## Security Model

Security posture focuses on bounded localhost operation, not hard sandbox claims.

Implemented controls:

- session-based login/logout
- password hash storage only
- session expiry and idle timeout enforcement
- recent re-authentication for sensitive operations
- CSRF protection on dangerous POST routes
- CSP, frame denial, no-referrer, and no-sniff headers
- session secret generated on first run and stored outside the repository when not supplied by env
- short-lived interactive CLI authentication for dangerous CLI actions
- approval snapshot binding with stale detection
- explicit execution manifest hashes across proposal, approval, queue, and history
- dedicated worker queue and lease-based execution claiming
- bounded system actions instead of raw PowerShell or raw shell execution
- workspace-first filesystem allowlist
- protected writes blocked for source, settings, DB, audit, log, and secret targets
- symlink traversal blocking
- HTTP connector policy on scheme, host, port, redirects, timeout, and response size
- provider egress policy on host allowlists and restricted-data handling
- protected local blob storage for sensitive payload fields with DPAPI when available
- fail-closed blob storage for sensitive payloads unless strong protection exists or an explicit insecure override is enabled
- hash-chained audit trail with verification

Important limit:

This is still not a kernel sandbox or an OS isolation boundary.

## Approval Model

Approvals are bound to executable snapshots, not just human-readable summaries.

Every approval is tied to:

- proposal snapshot hash
- execution manifest hash
- action payload hash
- policy evaluation hash
- settings version hash
- resource precondition hash

Execution verifies the approved snapshot again before running. If the live state no longer matches, the proposal becomes `stale` and requires re-approval.

The proposal detail page surfaces:

- why the action was proposed
- affected resources
- preview and diff where available
- snapshot hashes
- execution manifest hash
- stale drift warnings
- rollback indicator
- approval log with bound hashes

## Workspace Model

Default runtime state on Windows:

- `%LOCALAPPDATA%\\WinAgentRuntime\\data`
- `%LOCALAPPDATA%\\WinAgentRuntime\\secrets`
- `%LOCALAPPDATA%\\WinAgentRuntime\\logs`
- `%LOCALAPPDATA%\\WinAgentRuntime\\workspace`

Default behavior:

- relative paths resolve inside the dedicated workspace
- writes outside the allowlist are denied
- source tree writes are denied
- DB, audit, settings, token, and log file writes are denied
- symlink traversal is denied
- sensitive payload bodies are externalized into protected local blob storage instead of being duplicated across runtime tables
- restricted and privileged-sensitive blobs fail closed unless DPAPI is available or an insecure local override is explicitly enabled

This is designed to let operators work with local files without granting broad write access to the repository itself.

## Provider Model

Honest provider support:

- `mock`
- `openai`
- `openai-compatible`
- `generic-http`
- `anthropic`
- `gemini`

Provider orchestration features:

- provider-specific catalog records persisted in SQLite
- provider-specific enabled / disabled state
- provider-specific base URL, model default, auth env name, and allowed-host list
- capability metadata
- health status
- retry policy
- circuit breaker behavior
- fallback chain
- routing profiles: `fast`, `cheap`, `strong`, `local-only`, `privacy-preferred`
- summary/planning profile selection
- provider egress allowlist
- restricted-data refusal unless explicitly overridden

Auth model:

- environment variables only
- no hardcoded secrets
- no fake subscription-token reuse

## Execution Worker Model

The web process does not directly run dangerous actions.

Execution flow:

1. approval creates a queue record
2. worker claims a job atomically and binds it to a specific approval record
3. worker takes a lease and records an execution attempt
4. executor validates the exact queued approval hashes again
5. bounded connector action runs
6. result or failure is written to history and audit

Worker safety improvements:

- atomic job claim
- attempt counters
- lease and heartbeat fields
- stale running job recovery
- dead-letter handling after repeated failures

## Windows-First Setup

Requirements:

- Windows 10 or Windows 11
- Python 3.13 recommended
- PowerShell
- no Docker
- no WSL required

Bootstrap:

```powershell
git clone <your-repo-url>
cd win-agent-runtime
.\scripts\bootstrap.ps1
```

Run localhost UI:

```powershell
.\scripts\run-local.ps1
```

Development hot reload is optional:

```powershell
.\scripts\run-local.ps1 -Reload
```

Run worker:

```powershell
.\scripts\run-worker.ps1
```

Optional Windows startup task for the localhost console:

```powershell
.\scripts\install-console-startup-task.ps1
```

Open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Localhost Usage

Suggested first-run flow:

1. Open `/setup`
2. Create the first operator account
3. Open the Assistant Workbench
4. Enter a natural language goal
5. Review the task graph and proposals
6. Approve or reject specific steps
7. Let the worker execute approved work
8. Inspect history, audit verification, and snapshot status

## CLI

Read-only examples:

```powershell
python -m app.cli run-agent "Review notes and prepare a safe task" --path notes.txt --task-title "Review notes"
python -m app.cli list-proposals
python -m app.cli list-providers
python -m app.cli verify-audit
```

Sensitive commands require interactive local authentication or a protected short-lived token file. Passwords are not accepted as command-line arguments:

```powershell
python -m app.cli approve-proposal <proposal_id> --reason "Approved" --username <user>
python -m app.cli reject-proposal <proposal_id> --reason "Rejected" --username <user>
python -m app.cli run-worker --once --username <user>
```

Advanced automation can mint a short-lived purpose-scoped token into protected local storage:

```powershell
python -m app.cli issue-cli-token --username <user> --purpose worker --token-file worker.token
python -m app.cli run-worker --once --token-file worker.token
```

`.\scripts\run-worker.ps1` asks only for the operator username in PowerShell and lets the Python CLI prompt for the password securely, so the password does not live in PowerShell argv or a long-lived shell variable. Protected token-file mode remains an advanced path only.

## Testing

Run the test suite:

```powershell
.\scripts\test.ps1
```

Current automated coverage includes:

- auth bootstrap, logout, and idle timeout
- CSRF enforcement
- CLI auth prompt/token-file requirements
- approval snapshot stale detection
- execution manifest binding
- worker execution and stale job reclaim
- filesystem allowlist and symlink blocking
- bounded system action policy
- provider fallback and restricted egress behavior
- protected storage fail-closed behavior
- release packaging allowlist verification
- audit integrity verification
- multi-agent run, handoff, and task-node persistence

## Release Hygiene

The repository is prepared to avoid accidental runtime leakage, and release archives should be built from the allowlist packager instead of zipping the working tree:

- `.venv/` ignored
- `data/` ignored
- legacy repo-local workspace directories ignored for cleanup safety
- `workspace/` ignored
- `.env` ignored
- runtime tokens and audit files ignored
- example config only, no live secrets committed

Build and verify a clean archive:

```powershell
.\scripts\package-release.ps1 -Version vnext -VerifyWorkingTree -Clean
```

CI-oriented verification mode:

```powershell
.\scripts\package-release.ps1 -Version vnext -CI
```

Every clean archive now contains `release_manifest.json` with build time, included paths, excluded path policy, and the git revision when available.

Clean ignored repo-local caches and legacy runtime folders when needed:

```powershell
.\scripts\clean-local-artifacts.ps1
```

Before publishing a release, verify:

- no local runtime DB is tracked
- no session secret or protected secret blob is tracked
- tests pass
- README and docs match shipped behavior

## Roadmap

Planned next steps:

- stronger Windows secret storage, including optional Credential Manager support
- richer Windows notifications and true service-mode worker isolation
- per-user RBAC and approval scopes
- stronger worker isolation options
- richer rollback helpers for selected file operations
- packaged Windows release flow

## Contributing

Contributions should preserve the trust model:

- do not reintroduce raw shell execution
- do not bypass auth, CSRF, approval, or audit
- keep Windows localhost setup simple
- add tests for new policy or execution behavior
- document safety-relevant changes

## License

MIT. See [LICENSE](D:/User_Data/Documents/Playground/win-agent-runtime/LICENSE).
