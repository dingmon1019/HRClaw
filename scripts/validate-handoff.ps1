param()

$scriptPath = Join-Path $PSScriptRoot "package-release.ps1"
& $scriptPath -Mode "handoff" -ValidateSourceOnly
