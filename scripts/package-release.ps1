param(
    [string]$Version = "local",
    [switch]$VerifyWorkingTree,
    [switch]$AllowDirtyWorkingTree,
    [switch]$Clean,
    [switch]$CI,
    [string]$VerifyArchive
)

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}

$args = @(".\scripts\package_release.py", "--version", $Version)
if ($VerifyArchive) {
    $args = @(".\scripts\package_release.py", "--verify-archive", $VerifyArchive)
}
if ($VerifyWorkingTree) {
    $args += "--verify-working-tree"
}
elseif (-not $AllowDirtyWorkingTree -and -not $VerifyArchive) {
    $args += "--verify-working-tree"
}
if ($AllowDirtyWorkingTree) {
    $args += "--allow-dirty-working-tree"
}
if ($Clean) {
    $args += "--clean"
}
if ($CI) {
    $args += "--ci"
}

& $python @args
