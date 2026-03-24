Add-Type -AssemblyName System.Windows.Forms

$runtimeRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "WinAgentRuntime"
}
else {
    Join-Path $HOME ".winagentruntime"
}

$initialDirectory = Join-Path $runtimeRoot "workspace"
if (-not (Test-Path $initialDirectory)) {
    New-Item -ItemType Directory -Path $initialDirectory -Force | Out-Null
}

$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.InitialDirectory = $initialDirectory
$dialog.Filter = "All files (*.*)|*.*"
$dialog.Multiselect = $false

if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.FileName
}
