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

## What Bootstrap Does

`.\scripts\bootstrap.ps1`:

- creates `.venv`
- installs dependencies
- copies `.env.example` to `.env` if missing
- creates `runtime_workspace\`
- creates `data\admin_token.txt` if missing

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
- worker CLI actions require a local admin token
- if `python` is not on PATH, use `py -3.13`
- `pywin32` is optional; the Outlook connector fails gracefully when unavailable

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
.\scripts\bootstrap.ps1
```

This creates `data\admin_token.txt`.

### Outlook connector unavailable

That is expected when Outlook or `pywin32` is missing. The rest of the runtime remains usable.
