param(
    [switch]$Workspace,
    [switch]$Logs,
    [switch]$Secrets
)

$runtimeRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "WinAgentRuntime"
}
else {
    Join-Path $HOME ".winagentruntime"
}

$targets = @()
if ($Workspace) {
    $targets += (Join-Path $runtimeRoot "workspace")
}
if ($Logs) {
    $targets += (Join-Path $runtimeRoot "logs")
}
if ($Secrets) {
    $targets += (Join-Path $runtimeRoot "secrets")
}
if (-not $targets) {
    $targets += $runtimeRoot
}

foreach ($target in $targets) {
    if (-not (Test-Path $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
    Start-Process explorer.exe $target
}

