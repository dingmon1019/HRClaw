param(
    [string]$TaskName = "WinAgentRuntimeWorker"
)

Stop-ScheduledTask -TaskName $TaskName
Write-Host "Stopped scheduled task $TaskName"
