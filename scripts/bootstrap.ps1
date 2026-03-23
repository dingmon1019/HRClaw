param(
    [string]$VenvPath = ".venv"
)

$runtimeRoot = Join-Path $env:LOCALAPPDATA "WinAgentRuntime"
$dataDir = Join-Path $runtimeRoot "data"
$logsDir = Join-Path $runtimeRoot "logs"
$secretsDir = Join-Path $runtimeRoot "secrets"
$workspaceDir = Join-Path $runtimeRoot "workspace"

function Get-PythonLauncher {
    $venvPython = Join-Path $VenvPath "Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & py -3.13 -m venv $VenvPath
        return (Join-Path $VenvPath "Scripts\python.exe")
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3.13 was not found. Install Python or make sure `py` or `python` is on PATH."
    }

    & python -m venv $VenvPath
    return (Join-Path $VenvPath "Scripts\python.exe")
}

$python = Get-PythonLauncher
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
}

foreach ($path in @($runtimeRoot, $dataDir, $logsDir, $secretsDir, $workspaceDir)) {
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}

Write-Host "Environment ready."
Write-Host "Activate with $VenvPath\\Scripts\\Activate.ps1"
Write-Host "Run app with .\\scripts\\run-local.ps1"
Write-Host "Run worker with .\\scripts\\run-worker.ps1"
Write-Host "Runtime state root: $runtimeRoot"
Write-Host "Workspace: $workspaceDir"
