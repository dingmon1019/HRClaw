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
2. Verify `git status` does not include secrets or runtime state.
3. Confirm docs match the shipped behavior.
4. Confirm provider credentials are referenced only by environment-variable names.
5. Confirm worker startup instructions do not recreate repo-local state.

This project is safer than a basic localhost agent wrapper, but it is still not an OS-level sandbox release. Release notes should stay honest about that boundary.
