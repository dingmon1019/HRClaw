param(
    [string]$TaskName = "WinAgentRuntime.Console",
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$runScript = Join-Path $projectRoot "scripts\run-local.ps1"
if (-not (Test-Path $runScript)) {
    throw "run-local.ps1 was not found under the project scripts directory."
}

$escapedRoot = $projectRoot.Replace("'", "''")
$escapedScript = $runScript.Replace("'", "''")
$arguments = "-NoProfile -WindowStyle Hidden -Command `"Set-Location -LiteralPath '$escapedRoot'; & '$escapedScript' -Host '$Host' -Port $Port`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Installed startup task '$TaskName' for the current Windows user."
