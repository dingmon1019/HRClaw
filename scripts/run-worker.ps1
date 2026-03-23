param(
    [switch]$Once,
    [int]$Limit = 1,
    [double]$Interval = 2.0
)

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}

$adminTokenPath = "data\admin_token.txt"
if (-not $env:WIN_AGENT_ADMIN_TOKEN -and (Test-Path $adminTokenPath)) {
    $env:WIN_AGENT_ADMIN_TOKEN = (Get-Content $adminTokenPath -Raw).Trim()
}

if (-not $env:WIN_AGENT_ADMIN_TOKEN) {
    throw "Admin token not found. Run .\scripts\bootstrap.ps1 first or set WIN_AGENT_ADMIN_TOKEN."
}

if ($Once) {
    & $python -m app.cli run-worker --once --admin-token $env:WIN_AGENT_ADMIN_TOKEN
    exit $LASTEXITCODE
}

& $python -m app.cli run-worker --limit $Limit --interval $Interval --admin-token $env:WIN_AGENT_ADMIN_TOKEN
