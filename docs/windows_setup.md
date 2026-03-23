# Windows Setup

## Target Environment

Supported target:

- Windows 10
- Windows 11

No Docker is required.
No WSL is required.

## Prerequisites

- Python 3.13 recommended
- PowerShell
- Git

Optional:

- Outlook desktop plus `pywin32` if you want the Outlook connector

## Fast Start

```powershell
git clone <your-repo-url>
cd win-agent-runtime
.\scripts\bootstrap.ps1
.\scripts\run-local.ps1
```

In a second PowerShell window:

```powershell
cd win-agent-runtime
.\scripts\run-worker.ps1
```

Open:

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Runtime State Layout

By default the runtime keeps live state outside the repository:

- `%LOCALAPPDATA%\WinAgentRuntime\data`
- `%LOCALAPPDATA%\WinAgentRuntime\logs`
- `%LOCALAPPDATA%\WinAgentRuntime\secrets`
- `%LOCALAPPDATA%\WinAgentRuntime\workspace`

That means the local database, audit mirror, generated session secret, and protected payload blobs should not live under the cloned git working tree unless you explicitly override paths.

## What Bootstrap Does

`.\scripts\bootstrap.ps1`:

- creates `.venv`
- installs dependencies
- copies `.env.example` to `.env` if missing
- initializes `%LOCALAPPDATA%\WinAgentRuntime\`
- creates `data\`, `logs\`, `secrets\`, and `workspace\` under that runtime root

## Manual Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Then:

```powershell
.\scripts\run-local.ps1
.\scripts\run-worker.ps1
```

## Windows Notes

- the UI is loopback-bound by default
- worker CLI actions require a short-lived CLI auth token issued after password verification
- if `python` is not on PATH, use `py -3.13`
- `pywin32` is optional; the Outlook connector fails gracefully when unavailable
- when `pywin32` is installed, DPAPI-backed local secret protection is used automatically

## Troubleshooting

### `python` is not recognized

Use:

```powershell
py -3.13 --version
```

If that works, either use `py -3.13` directly or add Python to the Windows user PATH.

### Worker token errors

If `run-worker.ps1` reports a missing token, run:

```powershell
.\scripts\run-worker.ps1
```

The script can prompt for local credentials, mint a short-lived token for the `worker` purpose, and keep it only in the current PowerShell process environment.

### Outlook connector unavailable

That is expected when Outlook or `pywin32` is missing. The rest of the runtime remains usable.
