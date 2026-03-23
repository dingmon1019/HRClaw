param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload
)

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}

$args = @("-m", "uvicorn", "main:app", "--host", $Host, "--port", "$Port")
if ($Reload) {
    $args += "--reload"
}

& $python @args
