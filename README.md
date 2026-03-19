# win-agent-runtime

Windows-first local agent runtime inspired by NemoClaw-style approval safety and architecture, with multi-provider model support in the spirit of OpenClaw.

This project is inspired by those ideas. It is not an official NemoClaw implementation and it does not claim official OpenClaw compatibility.

## Purpose

`win-agent-runtime` is a production-oriented local agent runtime for Windows 10/11 that:

- runs entirely on localhost
- persists proposals, approvals, summaries, and history in SQLite
- requires explicit approval before execution
- enforces allowlist-based policy controls
- supports multiple model providers behind a shared abstraction
- exposes both a FastAPI API and a desktop-friendly Jinja2 + HTMX operator UI

The project is designed to be usable as a local agent control plane, a foundation for internal tooling, or a starting point for a safer open-source agent workstation.

## Architecture

High-level execution flow:

1. Collect local context through read-safe connectors.
2. Summarize the collected context with the selected provider or a local fallback.
3. Propose structured actions.
4. Persist proposals in SQLite.
5. Wait for operator approval.
6. Execute through the connector dispatcher.
7. Audit the approval, execution, and outcome.

Core principles:

- Agent suggests, never executes blindly.
- Side-effecting actions require approval.
- Policy controls every execution path.
- Runtime is local-first and localhost-native.
- Connectors are modular and pluggable.
- Provider integrations are abstracted behind a registry and shared interface.

Project layout:

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

Additional architecture notes live in [docs/architecture.md](docs/architecture.md).

## Features

### Safety model

- Safe mode and relaxed mode
- Allowlisted filesystem roots
- Allowlisted HTTP hosts
- Allowlisted PowerShell commands
- Pending proposal queue
- Approval and rejection audit trail
- Action execution history
- Optional JSON audit log
- Extensible shape for future RBAC

### Connectors

Required connectors included:

- Filesystem connector
- HTTP connector
- Local task connector

Additional connectors:

- PowerShell system connector
- Optional Outlook connector via `pywin32`

The Outlook connector fails gracefully when `pywin32` is not installed.

### Providers

Built-in providers:

- `mock`
- `openai`
- `openai-compatible`
- `generic-http`
- `anthropic`
- `gemini`

Provider capabilities:

- provider selection
- model selection
- fallback provider
- retry handling
- timeout handling
- environment-variable authentication

No API keys are hardcoded. Credentials are loaded from environment variables referenced by config.

## Database model

Primary tables:

- `proposals`
- `approvals`
- `action_history`
- `summaries`
- `connector_runs`
- `settings`

Additional local runtime table:

- `tasks`

## Windows setup

Requirements:

- Windows 10 or Windows 11
- Python 3.11+ recommended
- PowerShell

Create and install:

```powershell
git clone <your-repo-url>
cd win-agent-runtime
.\scripts\bootstrap.ps1
```

Or install manually:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Localhost usage

Run the app:

```powershell
python main.py
```

Or:

```powershell
.\scripts\run-local.ps1
```

Open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

Available pages:

- Dashboard
- Run Agent
- Proposals Inbox
- Proposal Detail
- Approval UI
- Action History
- Connectors Status
- Settings

## Configuration

Example `.env` values:

```env
PROVIDER=openai
MODEL=gpt-5
BASE_URL=http://127.0.0.1:8001/v1
API_KEY_ENV=OPENAI_API_KEY
FALLBACK_PROVIDER=mock
RUNTIME_MODE=safe
ALLOWED_FILESYSTEM_ROOTS=.
ALLOWED_HTTP_HOSTS=127.0.0.1,localhost
POWERSHELL_ALLOWLIST=Get-ChildItem,Get-Content,Test-Path,Resolve-Path,Get-Date
```

Persistent runtime settings are stored in SQLite and can be updated through the Settings page.

## Provider system

Providers are registered through `app/providers/registry.py` and accessed through `ProviderService`.

Design goals:

- swap providers without changing runtime code
- support OpenAI and OpenAI-compatible APIs
- support custom HTTP gateways
- keep test coverage strong with the mock provider
- make future provider adapters easy to add

Selection order:

1. explicit request override
2. configured default provider
3. configured fallback provider

## Connector model

Connectors are isolated execution units that expose:

- `healthcheck()`
- `collect()`
- `execute()`

Each proposal names its connector and action type, for example:

- `filesystem.read_text`
- `filesystem.write_text`
- `http.get`
- `task.create`
- `system.powershell`
- `outlook.send_mail`

## CLI

Run through the CLI with:

```powershell
python -m app.cli run-agent "Inspect .\docs and create a task" --path .\docs --task-title "Review docs"
python -m app.cli list-proposals
python -m app.cli approve-proposal <proposal_id>
python -m app.cli reject-proposal <proposal_id>
python -m app.cli show-history
python -m app.cli list-providers
python -m app.cli test-provider --provider mock
```

## Testing

Run the test suite:

```powershell
.\scripts\test.ps1
```

Or:

```powershell
pytest -q
```

Covered areas include:

- policy enforcement
- proposal lifecycle
- SQLite persistence
- provider fallback behavior
- basic API routes

## Roadmap

- RBAC and per-role approval policies
- batch approvals and approval policies by risk class
- richer planning prompts and tool schemas
- connector pack expansion
- more detailed task orchestration memory
- packaged Windows installer
- vendored HTMX asset for fully offline UI delivery

## Contributing

1. Create a branch with the `codex/` prefix or your team convention.
2. Keep safety guarantees intact: no bypasses around approvals, policy, or audit.
3. Add tests for behavior changes.
4. Document new providers or connectors in the README and docs.
5. Prefer small, reviewable pull requests.

Issues and pull requests are welcome.

## License

MIT. See [LICENSE](LICENSE).
