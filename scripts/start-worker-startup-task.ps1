param(
    [string]$TaskName = "WinAgentRuntime.Worker"
)

Start-ScheduledTask -TaskName $TaskName
Write-Host "Started scheduled task $TaskName"
