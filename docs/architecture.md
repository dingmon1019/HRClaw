# Architecture

## Identity

Win Agent Runtime is a Windows-first local agent runtime inspired by NemoClaw-style approval safety and multi-provider ideas similar to OpenClaw.

It is not an official implementation of either project.
It is not an OS sandbox.

## Core Runtime Loop

The runtime follows a bounded planning and execution loop:

1. collect local context
2. summarize with a selected provider profile
3. build structured action proposals
4. evaluate proposals through policy
5. persist proposals in SQLite
6. wait for operator approval
7. enqueue approved proposals
8. execute queued work in the worker
9. audit every state transition

## Major Components

### Web/UI Process

Responsibilities:

- session auth
- CSRF protection
- login / logout / setup
- operator dashboard
- proposal review
- settings management
- proposal approval or rejection
- queue creation

Non-responsibilities:

- direct execution of dangerous connector actions

### Worker Process

Responsibilities:

- poll `execution_jobs`
- mark jobs running
- dispatch approved work through connectors
- record result, failure, or policy block
- emit worker audit events

### Policy Layer

The policy engine evaluates proposals before they are shown and again before execution.

Current enforcement includes:

- safe vs relaxed mode behavior
- filesystem workspace allowlist
- protected path blocking
- bounded system action validation
- HTTP scheme / host / port restrictions
- private-network HTTP blocking by default
- connector enable / disable checks

## Authentication Model

Local auth is session-based:

- initial operator account is bootstrapped through `/setup`
- password hashes use PBKDF2-SHA256
- plaintext passwords are never stored
- sessions are cookie-backed
- recent-auth timestamps support sensitive re-auth flows

Sensitive actions requiring recent auth:

- high-risk approvals
- settings changes
- settings import / reset

## CSRF Model

All dangerous POST routes require a CSRF token:

- HTML forms use hidden form fields
- API POST calls use `x-csrf-token`

## Filesystem Model

The runtime is workspace-first:

- default workspace root is `workspace/`
- relative paths resolve inside the workspace
- repository source and runtime state are protected from writes
- symlink traversal is rejected

## HTTP Model

HTTP connector policy is intentionally strict:

- scheme allowlist
- host allowlist
- port allowlist
- redirect blocking by default
- request timeout enforcement
- response size limit
- localhost/private-network blocking unless explicitly enabled

## Provider Orchestration

Providers are registered adapters with common metadata:

- configuration status
- local/remote capability
- routing profiles
- retry behavior
- circuit breaker state

Routing profiles:

- `fast`
- `cheap`
- `strong`
- `local-only`

## Audit Model

Audit events are written to both:

- SQLite `audit_entries`
- optional JSONL audit log

Each audit entry stores:

- event type
- payload
- previous hash
- current entry hash
- timestamp

This supports post-run audit integrity verification.

## Data Stores

SQLite stores:

- proposals
- approvals
- action history
- summaries
- connector runs
- settings
- users
- execution jobs
- audit entries
- tasks

## Current Limits

The current architecture improves local operator control, but it still has important limits:

- worker isolation is process-level, not kernel-level
- Windows Credential Manager integration is not implemented
- rollback is advisory, not automatic
- worker supervision is manual
- RBAC is planned, not shipped
