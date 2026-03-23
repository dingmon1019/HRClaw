# win-agent-runtime

`win-agent-runtime` is a Windows-first local agent runtime inspired by NemoClaw-style approval safety and architecture, with multi-provider orchestration in the spirit of OpenClaw.

It is not an official NemoClaw implementation.
It does not claim OpenClaw compatibility.
It is not OpenShell-level sandboxing.

This repository is aimed at practical localhost operator control on Windows 10/11:

- proposal-first execution
- approval gating
- session-based localhost auth
- tamper-evident audit logging
- bounded system actions instead of raw shell execution
- multi-provider routing with fallback and circuit breaking
- FastAPI + Jinja2 + HTMX UI
- SQLite persistence

## What This Project Is

This project is a local operator runtime for structured agent actions.

It collects context, summarizes it, proposes actions, waits for approval, queues approved work, and executes that work through a separate worker path.

The design goal is not “autonomous execution at all costs”.
The design goal is controlled local operation with durable auditability.

## What This Project Is Not

This project is not:

- a kernel sandbox
- a container runtime
- a privilege isolation boundary comparable to OpenShell-like systems
- a remote multi-tenant control plane
- a replacement for OS-level sandboxing or endpoint security

If you need hard isolation from the host OS, you still need stronger sandboxing than this repository provides today.

## Security Model

The v2 security posture is built around bounded local trust rather than blind execution:

- localhost binding stays on `127.0.0.1` by default
- operator login/logout uses session-based auth
- only password hashes are stored
- dangerous pages and mutation endpoints require authentication
- sensitive actions require recent re-authentication
- CSRF tokens are enforced on POST forms and dangerous POST APIs
- raw PowerShell execution has been removed
- bounded system actions replace freeform shell commands
- filesystem access is constrained to a dedicated workspace root
- writes to source, config, database, and audit paths are blocked
- symlink traversal and path escape are blocked
- HTTP requests are constrained by scheme, host, port, timeout, redirect, and response-size rules
- localhost/private-network HTTP access is blocked unless explicitly enabled
- audit entries are hash-chained and verifiable

## Approval Model

Core flow:

1. collect
2. summarize
3. propose
4. approve or reject
5. queue
6. execute in worker
7. audit

Important behavior:

- the planner does not directly execute side effects
- the web UI does not directly execute approved work
- approvals queue jobs into SQLite
- the worker consumes queued jobs and records success, failure, or policy block
- proposal detail pages surface policy notes, affected resources, previews, diffs, and rollback guidance

## Execution Worker Model

Execution is intentionally separated from proposal generation:

- FastAPI / UI process handles login, planning, review, and approvals
- approved work is written to the `execution_jobs` queue
- a worker process consumes queued jobs
- worker lifecycle events are audit logged
- worker results are written to job history and action history

This is not a sandbox, but it is a much safer separation of concerns than direct execution inside the web request path.

## Workspace Safety Model

The runtime no longer defaults to the project root for filesystem operations.

Default workspace:

- `workspace/`

Default policy:

- relative paths resolve inside the workspace root
- source code and repository files are not valid write targets
- database and audit files are not valid write targets
- session secret storage is not a valid write target

This means the operator can work with local files without giving the runtime open write access to the repository itself.

## Provider Model

Providers are abstracted behind a shared interface and registry.

Built-in adapters:

- `mock`
- `openai`
- `openai-compatible`
- `generic-http`
- `anthropic`
- `gemini`

Provider orchestration features:

- explicit provider selection
- profile-based routing: `fast`, `cheap`, `strong`, `local-only`
- fallback provider chain
- retry policy
- timeout handling
- provider capability metadata
- circuit breaker state
- provider health status in the UI

Credentials are loaded from environment variables only.
No API keys are hardcoded.
No subscription-token reuse claims are made.

## Connectors

Included connectors:

- filesystem
- http
- task
- system
- outlook (optional, `pywin32`)

System connector actions are bounded and schema-driven:

- `system.list_directory`
- `system.read_text_file`
- `system.test_path`
- `system.get_time`

Removed behavior:

- raw freeform PowerShell command execution

## Repository Layout

```text
project_root/
├── app/
│   ├── api/
│   ├── audit/
│   ├── config/
│   ├── connectors/
│   ├── core/
│   ├── memory/
│   ├── policy/
│   ├── providers/
│   ├── runtime/
│   ├── schemas/
│   ├── security/
│   └── services/
├── docs/
├── examples/
├── scripts/
├── tests/
├── ui/
│   ├── partials/
│   ├── static/
│   └── templates/
├── .env.example
├── LICENSE
├── README.md
├── main.py
└── requirements.txt
```

More detail is in [docs/architecture.md](docs/architecture.md).

## Database Tables

Core tables:

- `proposals`
- `approvals`
- `action_history`
- `summaries`
- `connector_runs`
- `settings`
- `users`
- `execution_jobs`
- `audit_entries`
- `tasks`

## Windows Setup

Requirements:

- Windows 10 or Windows 11
- Python 3.13 recommended
- PowerShell
- no Docker
- no Linux or WSL requirement

Bootstrap:

```powershell
git clone <your-repo-url>
cd win-agent-runtime
.\scripts\bootstrap.ps1
```

Manual setup:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Localhost Usage

Start the web app:

```powershell
.\scripts\run-local.ps1
```

Start the worker in a second terminal:

```powershell
.\scripts\run-worker.ps1
```

Or process one queued job:

```powershell
.\scripts\run-worker.ps1 -Once
```

Open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

First-run flow:

1. open `/setup`
2. create the first operator account
3. log in
4. run the planner
5. review proposals
6. approve or reject
7. run the worker
8. inspect history and audit status

## CLI

Examples:

```powershell
python -m app.cli run-agent "Inspect workspace and create a task" --path notes.txt --task-title "Review notes"
python -m app.cli list-proposals
python -m app.cli approve-proposal <proposal_id> --reason "Approved for worker queue"
python -m app.cli run-worker --once
python -m app.cli list-jobs
python -m app.cli verify-audit
python -m app.cli list-providers
python -m app.cli test-provider --provider mock
```

## Settings Behavior

The settings page is no longer a raw config dump.

It now includes:

- safe vs sensitive grouping
- provider test flow
- audit integrity status
- explicit warnings for unsafe toggles
- reset-to-safe-defaults action
- sanitized export/import
- secret-environment presence without exposing secret values

Secrets remain environment-variable based.
Windows Credential Manager integration is not implemented yet.

## Testing

Run the test suite:

```powershell
.\scripts\test.ps1
```

Current automated coverage includes:

- authentication bootstrap and logout flow
- CSRF enforcement
- proposal queue lifecycle
- worker execution path
- policy hardening
- audit verification
- provider fallback and circuit breaker behavior
- SQLite persistence

## Honest Limitations

- no kernel sandboxing
- no RBAC yet
- no automatic rollback engine
- no secret storage integration beyond environment variables
- no background worker supervisor included
- HTTP policy is stricter than some local workflows and may need explicit opt-in changes

## Roadmap

Planned next steps:

- RBAC and per-role approval scopes
- Windows Credential Manager support
- stronger worker isolation options
- background worker supervision on Windows
- richer diff previews and rollback helpers
- improved provider health probes
- packaged Windows installer
- stronger local connector isolation

## Contributing

Contributions should preserve these boundaries:

- no blind execution paths
- no reintroduction of raw shell execution
- no secret leakage into UI or audit logs
- no bypass around auth, CSRF, approval, queueing, or audit

Recommended contribution flow:

1. create a branch
2. add or update tests
3. document behavior changes
4. keep the Windows localhost workflow working without Docker

## License

MIT. See [LICENSE](LICENSE).
