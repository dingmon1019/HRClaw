param(
    [string]$TaskName = "WinAgentRuntimeWorker"
)

Start-ScheduledTask -TaskName $TaskName
Write-Host "Started scheduled task $TaskName"
