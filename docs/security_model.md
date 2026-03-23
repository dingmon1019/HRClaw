# Security Model

## Security Goal

The goal is safer localhost operation, not a claim of hard sandboxing.

The runtime tries to reduce accidental or invisible side effects through:

- explicit authentication
- approval gates
- immutable snapshot binding
- bounded connectors
- egress controls
- separate worker execution
- tamper-evident audit logs

## Authentication

Implemented:

- initial setup flow for the first operator
- PBKDF2-SHA256 password hashes
- server-side session records with an opaque session ID in the browser cookie
- session age enforcement
- idle timeout enforcement
- recent re-authentication window for sensitive actions
- short-lived CLI authentication tokens issued only after password verification for dangerous CLI operations

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

- action payload hash
- policy hash
- settings hash
- resource precondition hash
- snapshot hash

Before execution, the worker verifies the exact approval record attached to the queued job, then recalculates a live snapshot. If the live state no longer matches the queued approval hashes, the proposal becomes `stale` and execution is blocked.

## Connector Safety

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

## Audit

Audit entries are append-oriented and hash chained.

Each entry stores:

- event type
- payload JSON
- previous hash
- current entry hash
- timestamp

DB audit remains enabled even when JSON file mirroring is disabled.

## Sensitive Local Storage

When a proposal payload includes sensitive text fields such as file content or HTTP request bodies, the runtime stores only digests and previews in the main proposal row. Full raw values are externalized into protected local blob storage under the secrets runtime directory.

On Windows with `pywin32` available, DPAPI protection is used. Without it, the runtime falls back to plain local storage and the UI warns about that weaker mode.

## Remaining Limits

- no kernel isolation
- no process sandboxing beyond role separation and worker boundary
- no hardware-backed secret storage yet
- no RBAC yet
- no signed audit export bundle yet
