param(
    [string]$TaskName = "WinAgentRuntime.Worker"
)

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName

[pscustomobject]@{
    TaskName = $task.TaskName
    State = [string]$task.State
    LastRunTime = [string]$info.LastRunTime
    NextRunTime = [string]$info.NextRunTime
    LastTaskResult = $info.LastTaskResult
} | ConvertTo-Json -Compress
