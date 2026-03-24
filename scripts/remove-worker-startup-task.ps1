param(
    [string]$TaskName = "WinAgentRuntime.Worker"
)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed worker startup task '$TaskName'."
}
else {
    Write-Host "Worker startup task '$TaskName' was not present."
}
