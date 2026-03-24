param(
    [string]$Version = "handoff",
    [switch]$Clean,
    [switch]$AllowDirtyWorkingTree
)

$scriptPath = Join-Path $PSScriptRoot "package-release.ps1"
$args = @(
    "-Version", $Version,
    "-Mode", "handoff"
)
if ($Clean) {
    $args += "-Clean"
}
if ($AllowDirtyWorkingTree) {
    $args += "-AllowDirtyWorkingTree"
}

& $scriptPath @args
