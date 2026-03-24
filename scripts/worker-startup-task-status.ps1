param(
    [string]$TaskName = "WinAgentRuntimeWorker"
)

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName

[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    LastRunTime = $info.LastRunTime
    NextRunTime = $info.NextRunTime
    LastTaskResult = $info.LastTaskResult
} | Format-List
