param(
    [string]$AppRoot
)

$runtimeRoot = if ($AppRoot) {
    $AppRoot
}
elseif ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "WinAgentRuntime"
}
else {
    Join-Path $HOME ".winagentruntime"
}

$secretsDir = Join-Path $runtimeRoot "secrets"
$logsDir = Join-Path $runtimeRoot "logs"
$workspaceDir = Join-Path $runtimeRoot "workspace"
$dataDir = Join-Path $runtimeRoot "data"
$sessionSecret = Join-Path $secretsDir "session_secret.bin"

Write-Host "Runtime Root : $runtimeRoot"
Write-Host "Data Dir     : $dataDir"
Write-Host "Logs Dir     : $logsDir"
Write-Host "Secrets Dir  : $secretsDir"
Write-Host "Workspace Dir: $workspaceDir"
Write-Host ""

if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        $protection = & py -c "import importlib.util; print('dpapi-available' if importlib.util.find_spec('win32crypt') else 'dpapi-unavailable')" 2>$null
        Write-Host "Protection   : $protection"
    }
    catch {
        Write-Host "Protection   : unknown"
    }
}
else {
    Write-Host "Protection   : unknown"
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        $credManager = & py -c "import importlib.util; print('credential-manager-available' if importlib.util.find_spec('win32cred') else 'credential-manager-unavailable')" 2>$null
        Write-Host "Cred Store   : $credManager"
    }
    catch {
        Write-Host "Cred Store   : unknown"
    }
}
else {
    Write-Host "Cred Store   : unknown"
}

if (Test-Path $sessionSecret) {
    Write-Host "Session File : present"
}
else {
    Write-Host "Session File : not present"
}

Write-Host "Note         : live runtime state belongs outside the repository by default."
