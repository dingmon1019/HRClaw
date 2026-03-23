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
- prints the resolved Windows runtime paths so operators can verify where live state will land

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

Optional cleanup for repo-local caches and legacy runtime folders:

```powershell
.\scripts\clean-local-artifacts.ps1
```

Optional startup task for the localhost console:

```powershell
.\scripts\install-console-startup-task.ps1
.\scripts\remove-console-startup-task.ps1
```

Optional Explorer/runtime helpers:

```powershell
.\scripts\open-runtime-folders.ps1
.\scripts\show-runtime-posture.ps1
```

## Windows Notes

- the UI is loopback-bound by default
- worker CLI actions use a Python-side secure password prompt or a protected short-lived token file
- protected token-file mode is only available when strong local protection is active, or when an explicit insecure development override is enabled
- if `python` is not on PATH, use `py -3.13`
- `pywin32` is optional; the Outlook connector fails gracefully when unavailable
- when `pywin32` is installed, DPAPI-backed local secret protection is used automatically
- without DPAPI, generated session secrets, token-file mode, and sensitive blob storage fail closed unless `allow_insecure_local_storage` is explicitly enabled
- provider-specific catalog records let you keep local-model URLs and remote API settings separate inside the UI

## Troubleshooting

### `python` is not recognized

Use:

```powershell
py -3.13 --version
```

If that works, either use `py -3.13` directly or add Python to the Windows user PATH.

### Worker authentication errors

If `run-worker.ps1` reports an authentication issue, run:

```powershell
.\scripts\run-worker.ps1
```

The script asks for the operator username in PowerShell and the Python CLI prompts for the password securely, so the password does not travel on the command line or stay in a long-lived PowerShell variable.

### Release packaging fails on local repo artifacts

That is expected in the default release path. Clean the ignored repo-local artifacts first:

```powershell
.\scripts\clean-local-artifacts.ps1
.\scripts\package-release.ps1 -Version <tag> -Clean
```

If you are intentionally building a development smoke archive from a dirty tree, use:

```powershell
.\scripts\package-release.ps1 -Version smoke -AllowDirtyWorkingTree -Clean
```

### Outlook connector unavailable

That is expected when Outlook or `pywin32` is missing. The rest of the runtime remains usable.
