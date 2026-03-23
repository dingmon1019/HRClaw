param(
    [string]$VenvPath = ".venv"
)

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

if (-not (Test-Path "runtime_workspace")) {
    New-Item -ItemType Directory -Path "runtime_workspace" | Out-Null
}

if (-not (Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" | Out-Null
}

$adminTokenPath = "data\admin_token.txt"
if (-not (Test-Path $adminTokenPath)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $token = [Convert]::ToBase64String($bytes)
    Set-Content -Path $adminTokenPath -Value $token -NoNewline
}

Write-Host "Environment ready."
Write-Host "Activate with $VenvPath\\Scripts\\Activate.ps1"
Write-Host "Run app with .\\scripts\\run-local.ps1"
Write-Host "Run worker with .\\scripts\\run-worker.ps1"
Write-Host "CLI admin token stored at $adminTokenPath"
