param(
    [string]$TaskName = "WinAgentRuntime.Worker",
    [string]$TokenFile = "worker.token",
    [int]$Limit = 0,
    [double]$Interval = 2
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$runScript = Join-Path $projectRoot "scripts\run-worker.ps1"
if (-not (Test-Path $runScript)) {
    throw "run-worker.ps1 was not found under the project scripts directory."
}

$escapedRoot = $projectRoot.Replace("'", "''")
$escapedScript = $runScript.Replace("'", "''")
$escapedToken = $TokenFile.Replace("'", "''")
$arguments = "-NoProfile -WindowStyle Hidden -Command `"Set-Location -LiteralPath '$escapedRoot'; & '$escapedScript' -TokenFile '$escapedToken' -Limit $Limit -Interval $Interval`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Installed worker startup task '$TaskName' for the current Windows user."
Write-Host "This task expects a strongly protected CLI token file at startup."
