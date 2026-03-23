## Release Hygiene

This repository is intended to be publishable without bundling live runtime state.

Default expectations:

- no `.venv/` in releases
- no `.git/` metadata in release artifacts
- no live `.env` files in the repository
- no runtime SQLite database in the repository
- no session secret files in the repository
- no plaintext CLI bearer token files in the repository
- no audit mirrors or protected blob files in the repository

Runtime state belongs under the Windows local application data area by default:

- `%LOCALAPPDATA%\WinAgentRuntime\data`
- `%LOCALAPPDATA%\WinAgentRuntime\logs`
- `%LOCALAPPDATA%\WinAgentRuntime\secrets`
- `%LOCALAPPDATA%\WinAgentRuntime\workspace`

Before publishing:

1. Run the test suite.
2. Build the release archive with `.\scripts\package-release.ps1 -Version <tag> -VerifyWorkingTree -Clean`.
3. Verify `git status` does not include secrets or runtime state.
4. Confirm docs match the shipped behavior.
5. Confirm provider credentials are referenced only by environment-variable names.
6. Confirm worker startup instructions do not recreate repo-local state.

The release packager uses an allowlist and verifies the output archive. It does not zip the working tree blindly. Packaging now defaults to a working-tree preflight and fails unless you explicitly opt into `-AllowDirtyWorkingTree` for a smoke build.

A clean archive also includes `release_manifest.json` with:

- build time (UTC)
- version label
- included relative paths
- include policy
- excluded path policy
- git revision when available
- statement that runtime state belongs outside the repository

The packager also emits a `.sha256` sidecar for the produced archive.

CI-oriented packaging is supported:

```powershell
.\scripts\package-release.ps1 -Version <tag> -CI
```

Existing archives can be re-verified:

```powershell
.\scripts\package-release.ps1 -VerifyArchive .\dist\win-agent-runtime-<tag>.zip
```

If your local repo still contains ignored caches or legacy repo-scoped runtime folders, clean them before using `-VerifyWorkingTree`:

```powershell
.\scripts\clean-local-artifacts.ps1
```

For a development-only smoke archive from a contaminated tree:

```powershell
.\scripts\package-release.ps1 -Version smoke -AllowDirtyWorkingTree -Clean
```

Forbidden content for release archives includes:

- `.git`
- `.venv`
- `.codex-pkgs`
- `.pytest_cache`
- `data/`
- `runtime_workspace/`
- `workspace/`
- runtime DBs, audit logs, session secrets, and protected blob files

This project is safer than a basic localhost agent wrapper, but it is still not an OS-level sandbox release. Release notes should stay honest about that boundary.
