# Security Model

## Security Goal

The goal is safer localhost operation, not a claim of hard sandboxing.

The runtime tries to reduce accidental or invisible side effects through:

- explicit authentication
- approval gates
- immutable snapshot binding
- bounded connectors
- egress controls
- separate worker execution with task-scoped child-process bundles
- provider prompt governance that separates local-only collected context from outbound-safe provider context
- tamper-evident audit logs
- agent-scoped scratch roots that separate planner/reviewer/reporter/executor temporary work from the shared workspace

## Authentication

Implemented:

- initial setup flow for the first operator
- PBKDF2-SHA256 password hashes
- server-side session records with an opaque session ID in the browser cookie
- session age enforcement
- idle timeout enforcement
- recent re-authentication window for sensitive actions
- interactive short-lived CLI authentication for dangerous CLI operations
- Python-side secure password prompts for worker and approval CLI flows

Sensitive actions requiring recent re-auth:

- high-risk approvals
- settings changes
- settings import
- settings reset

## CSRF And Localhost Hardening

Implemented:

- CSRF token validation for dangerous POST routes
- trusted host validation
- origin validation for mutating requests
- request size enforcement
- CSP
- frame denial
- no-referrer
- no-sniff
- defensive exception handling without stack traces in templates

## Approval Binding

Approvals are bound to a stored snapshot containing:

- execution manifest hash
- action payload hash
- policy hash
- settings hash
- resource precondition hash
- snapshot hash

Before execution, the worker verifies the exact approval record attached to the queued job, then recalculates a live snapshot. If the live state no longer matches the queued approval hashes, the proposal becomes `stale` and execution is blocked.

The queue job and execution attempt also carry the execution bundle hash and boundary mode, so the operator can verify which constrained child-process bundle actually ran.

## Connector Safety

Planning no longer performs file-content reads, HTTP response fetches, or bounded system text reads by default. The planner works from operator-supplied inputs plus descriptor-only evidence, then creates explicit approval-gated proposals when more evidence is needed.

Run admission is graph-first. The runtime registers a graph run before summary generation or proposal materialization, so provider failures, protected-storage refusals, retry, and cancellation are recorded as durable graph-node outcomes rather than living only in request-path state.

### Filesystem

- dedicated workspace root
- runtime state stored under the Windows local app-data area by default, not inside the repository
- default deny outside allowlist
- protected writes denied for source, DB, audit, log, token, and env-like targets
- symlink traversal blocked
- bounded text preview limits
- full-file digests and canonical directory digests for stale approval detection

### System

Allowed actions only:

- `system.list_directory`
- `system.read_text_file`
- `system.test_path`
- `system.get_time`

There is no raw PowerShell execution path.

### HTTP

- scheme allowlist
- port allowlist
- host allowlist
- redirect control
- response size limit
- content-type checks
- localhost/private IP block by default

### Providers

- provider host allowlist
- private-network provider egress block by default
- restricted-data egress refusal by default
- retry and fallback still stay inside policy
- remote providers receive curated prompt variants instead of raw collected runtime context by default
- provider prompt posture is recorded in audit metadata so operators can verify what class of prompt left the machine

## Audit

Audit entries are append-oriented and hash chained.

Each entry stores:

- event type
- payload JSON
- previous hash
- current entry hash
- timestamp

DB audit remains enabled even when JSON file mirroring is disabled.

## Execution Boundary

Approved actions do not execute directly inside the web request or control-plane path.

Instead, the worker:

- claims the queued job
- rebuilds the approved execution bundle
- scrubs the child-process environment
- launches a dedicated child Python process for the bounded connector action
- narrows file and HTTP scope to exact task resources when the action schema allows it
- brokers task-connector actions through the parent process instead of handing the child a general runtime DB path
- records boundary metadata on the queue job and execution attempt

The current boundary is a constrained same-user child process:

- it does not inherit arbitrary parent `PYTHONPATH`
- it launches with isolated interpreter flags for more reproducible imports
- it carries exact granted file paths and HTTP targets where the action manifest allows it
- it starts inside an executor-specific scratch root under `%LOCALAPPDATA%\WinAgentRuntime\agent_workspaces`
- it keeps the worker lease alive with a heartbeat bridge during long-running child work
- it still runs as the same local Windows user account and is not an OS sandbox

This is a meaningful process boundary, but it is still not an OS sandbox.

## Sensitive Local Storage

When a proposal payload includes sensitive fields such as file content, HTTP bodies, headers, or task details, the runtime stores only digests and metadata in the main proposal row. Full raw values are externalized into protected local blob storage under the secrets runtime directory.

On Windows with `pywin32` available, DPAPI protection is used. Without it, the runtime reports `unprotected-local` posture and sensitive blob writes fail closed unless the operator explicitly enables insecure local storage override for development.

Derived runtime summaries follow the same rule. `EXTERNAL_OK` summaries may remain inline, but local-only or restricted summaries are stored as protected blobs when host protection is available and degrade to preview-only persistence when stronger at-rest protection is unavailable.

Session secrets, provider auth env references, and protected token files all live under the runtime secrets directory outside the repository by default.

When `pywin32` exposes `win32cred`, provider auth material can be stored in and resolved from Windows Credential Manager. Without strong local protection, new secret writes fail closed unless an explicit insecure development override is enabled.

Windows helper integrations are also capability-aware. If PowerShell or host support is unavailable, the dashboard, settings, and workspace-picker routes expose an explicit unsupported state instead of throwing a helper-launch error into the operator flow.

Startup no longer silently drains provider-backed graph work inline by default. In `background_preferred`, restart reconciliation restores graph state and queues ready work, but a worker must execute provider-backed planning nodes.

## Remaining Limits

- no kernel isolation
- no process sandboxing beyond role separation and the child-process worker boundary
- no restricted-token or AppContainer backend yet
- no hardware-backed secret storage yet
- no RBAC yet
- no signed audit export bundle yet
