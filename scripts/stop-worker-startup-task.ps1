param(
    [string]$TaskName = "WinAgentRuntime.Worker"
)

Stop-ScheduledTask -TaskName $TaskName
Write-Host "Stopped scheduled task $TaskName"
