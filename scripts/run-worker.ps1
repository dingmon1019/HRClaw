param(
    [switch]$Once,
    [int]$Limit = 1,
    [double]$Interval = 2.0
)

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}

if ($Once) {
    & $python -m app.cli run-worker --once
    exit $LASTEXITCODE
}

& $python -m app.cli run-worker --limit $Limit --interval $Interval
