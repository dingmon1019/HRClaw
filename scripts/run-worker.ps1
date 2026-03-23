param(
    [int]$Limit = 0,
    [double]$Interval = 2,
    [switch]$Once,
    [string]$Username,
    [string]$TokenFile
)

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}

if (-not $TokenFile -and -not $Username) {
    $Username = Read-Host "Operator username"
}

if ($TokenFile) {
    if ($Once) {
        & $python -m app.cli run-worker --once --token-file $TokenFile
        return
    }

    & $python -m app.cli run-worker --limit $Limit --interval $Interval --token-file $TokenFile
    return
}

if (-not $Username) {
    throw "Username is required when token-file mode is not used."
}

if ($Once) {
    & $python -m app.cli run-worker --once --username $Username
    return
}

& {
    & $python -m app.cli run-worker --limit $Limit --interval $Interval --username $Username
    }
