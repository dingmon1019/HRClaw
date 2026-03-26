# win-agent-runtime

`win-agent-runtime` is a Windows-first local multi-agent runtime and control console inspired by NemoClaw-style approval safety and OpenClaw-style provider flexibility.

It is not an official NemoClaw implementation.
It is not an official OpenClaw implementation.
It is not OpenShell-level kernel sandboxing.

The project is designed to be safer and more usable for Windows localhost operator workflows:

- assistant-style natural language workbench
- operator control console for approvals and audit
- proposal-first execution with approval snapshot binding
- separate worker execution boundary with task-scoped child-process bundles
- bounded local connectors only, no raw shell execution
- multi-provider routing with egress controls, scoring, and provider-specific governance records
- provider prompt governance that keeps raw local runtime context local by default and sends only curated outbound-safe summaries to remote providers
- agent-scoped scratch work areas with explicit promotion into the shared workspace
- artifact-lineage records for agent work-area assignment, scratch/shared writes, and approved promotion-style transfers
- graph-first run admission where decomposition, summary generation, review, merge, and reporting all execute as durable graph nodes
- explicit graph execution modes: `inline_compat`, `background_preferred`, and `background_only`
- integrated Windows workspace file picker in the workbench for managed workspace paths
- Windows scheduled-task controls in the settings console for installing, starting, stopping, and checking the worker background path
- optional Windows Credential Manager lifecycle for provider secrets when `pywin32` exposes `win32cred`
- SQLite-backed persistence, audit, task graph history, and clean release packaging

## Why This Exists

Many local agent stacks either optimize for raw autonomy or assume Linux-first tooling. This project takes the opposite approach:

- Windows 10/11 should be a first-class host
- localhost apps still need authentication, CSRF protection, and auditability
- approvals only matter if they are bound to what actually runs
- provider routing is part of the egress threat model
- provider governance should explain why a provider was chosen, not just which provider happened to answer
- an assistant UX should not force the operator to think in connector schemas first

## What This Project Is

This repository provides two integrated surfaces:

1. Assistant Workbench
   The operator gives a goal in natural language. The runtime decomposes it, shows agent collaboration, creates typed proposals, surfaces risks, and waits for approval.
2. Operator Control Console
   The operator reviews proposals, inspects previews and snapshot hashes, approves or rejects work, checks audit integrity, watches worker execution, and validates settings.

The runtime follows this high-level flow:

1. inspect operator-supplied inputs and safe descriptors only
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

- [architecture](docs/architecture.md)
- [multi-agent model](docs/multi_agent.md)
- [provider model](docs/provider_model.md)
- [security model](docs/security_model.md)
- [threat model](docs/threat_model.md)
- [Windows setup](docs/windows_setup.md)
- [release hygiene](docs/release_hygiene.md)

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
- fail-closed blob and secret-text storage unless strong protection exists or an explicit insecure override is enabled
- provider prompt curation so remote planning and reporting prompts receive sanitized summaries instead of raw local task or filesystem context by default
- derived local-only or restricted summaries are kept out of cleartext SQLite storage by using protected blobs or preview-only persistence when stronger protection is unavailable
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
- `%LOCALAPPDATA%\\WinAgentRuntime\\agent_workspaces`

Default behavior:

- relative paths resolve inside the dedicated workspace
- planner, reviewer, executor, and reporter each get an agent-scoped scratch root under `agent_workspaces`
- agent scratch paths are not the shared workspace; promotion into the shared workspace is explicit and approval-bound
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
- all enabled providers can enter the candidate pool when policy and capability checks allow it
- observed latency EWMA, success rate, failure streak, and last error category influence routing

Auth model:

- environment variables or optional Windows Credential Manager targets
- no hardcoded secrets
- no fake subscription-token reuse
- remote providers receive curated outbound-safe prompt variants when local-only data is present

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
- task-scoped execution bundles executed in a scrubbed child process
- child processes start inside an executor-specific scratch root rather than the runtime root
- exact granted file paths and HTTP targets recorded in boundary metadata when the action schema allows it
- brokered task actions so the child process does not receive a generic runtime DB path for task-connector work
- boundary metadata persisted on queue jobs, execution attempts, run history, and audit events

Important execution-boundary limit:

- this is process isolation only
- the child worker is not an OS sandbox
- the child worker still runs as the same local user account
- future Windows restricted-token or AppContainer style backends are not implemented yet

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

Graph execution now defaults to `background_preferred`. New objectives are admitted into the durable graph immediately, and planning may remain queued or running until a worker drains graph-node jobs. `inline_compat` remains available for compatibility flows that explicitly need synchronous planning completion.

Optional Windows worker/background helpers:

```powershell
.\scripts\install-worker-startup-task.ps1
.\scripts\remove-worker-startup-task.ps1
.\scripts\start-worker-startup-task.ps1
.\scripts\stop-worker-startup-task.ps1
.\scripts\worker-startup-task-status.ps1
.\scripts\pick-workspace-file.ps1
.\scripts\show-runtime-posture.ps1
```

In-product Windows integrations:

- the Assistant Workbench can open the native workspace file picker and return a workspace-relative path directly into the form
- the Settings page can install, start, stop, inspect, and remove the worker scheduled task
- provider catalog entries can check or rotate Windows Credential Manager targets without leaving the localhost UI
- non-Windows or missing-helper hosts now render explicit “unavailable on this host” states instead of failing dashboard/settings/workbench flows

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
5. Review the task graph, dependency edges, and proposals
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

`.\scripts\run-worker.ps1` asks only for the operator username in PowerShell and lets the Python CLI prompt for the password securely, so the password does not live in PowerShell argv or a long-lived shell variable. Protected token-file mode remains an advanced path only and now fails closed when strong local protection is unavailable unless an explicit insecure development override is enabled.

Provider secrets can also be created, tested, rotated, and deleted through Windows Credential Manager when `pywin32` exposes `win32cred`.

Credential Manager examples:

```powershell
python -m app.cli set-credential --target WinAgentRuntime/provider/openai
python -m app.cli test-credential --target WinAgentRuntime/provider/openai
python -m app.cli delete-credential --target WinAgentRuntime/provider/openai
```

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
- provider prompt governance and routing explanations
- protected storage fail-closed behavior
- release packaging allowlist verification
- audit integrity verification
- multi-agent run, handoff, task-node persistence, and graph scheduling

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

The clean packager is the only supported release path. Zipping the working tree directly is unsupported.

CI-oriented verification mode:

```powershell
.\scripts\package-release.ps1 -Version vnext -CI
```

Every clean archive now contains `release_manifest.json` with build time, include/exclude policy, git revision, and the explicit statement that runtime state belongs outside the repository. The packager also emits a `.sha256` sidecar for the archive.

For collaborator or Codex handoff bundles, use the dedicated handoff mode instead of reusing a release zip:

```powershell
.\scripts\export-handoff.ps1 -Version handoff -Clean
```

Handoff bundles use the same allowlist discipline, exclude repo-local runtime artifacts and stale `dist/` outputs, and label the manifest as `handoff-source`.

Validate a collaboration handoff source tree without creating an archive:

```powershell
.\scripts\validate-handoff.ps1
```

Clean ignored repo-local caches and legacy runtime folders when needed:

```powershell
.\scripts\clean-local-artifacts.ps1
```

If you intentionally need a development smoke archive from a contaminated working tree, you must opt in explicitly:

```powershell
.\scripts\package-release.ps1 -Version smoke -AllowDirtyWorkingTree -Clean
```

Before publishing a release, verify:

- no local runtime DB is tracked
- no session secret or protected secret blob is tracked
- tests pass
- README and docs match shipped behavior

## Roadmap

Planned next steps:

- stronger Windows worker isolation beyond the current same-user child-process boundary
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

MIT. See [LICENSE](LICENSE).
