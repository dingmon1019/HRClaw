param(
    [switch]$IncludeVenv,
    [switch]$IncludeDist
)

$targets = @(
    ".pytest_cache",
    "__pycache__",
    "data",
    "runtime_workspace",
    "workspace",
    "logs",
    "secrets",
    "protected_blobs",
    ".codex-pkgs",
    ".codex-venv"
)

if ($IncludeVenv) {
    $targets += ".venv"
}

if ($IncludeDist) {
    $targets += "dist"
}

foreach ($target in $targets) {
    if (Test-Path $target) {
        Remove-Item -Recurse -Force $target
        Write-Host "Removed $target"
    }
}

Write-Host "Local repo-scoped artifacts cleaned. Live runtime state under LocalAppData is untouched."
Write-Host "Recommended next step: .\\scripts\\package-release.ps1 -Version <tag> -Clean"
