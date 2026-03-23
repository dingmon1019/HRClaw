param(
    [switch]$Once,
    [int]$Limit = 1,
    [double]$Interval = 2.0,
    [string]$UserName = "operator"
)

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
}

if (-not $env:WIN_AGENT_CLI_TOKEN) {
    $credential = Get-Credential -UserName $UserName -Message "Issue a short-lived CLI token for the worker"
    $plainPassword = $credential.GetNetworkCredential().Password
    $tokenJson = & $python -m app.cli issue-cli-token --username $credential.UserName --password $plainPassword --purpose worker
    $tokenPayload = $tokenJson | ConvertFrom-Json
    $env:WIN_AGENT_CLI_TOKEN = $tokenPayload.token
}

if (-not $env:WIN_AGENT_CLI_TOKEN) {
    throw "CLI token issuance failed."
}

if ($Once) {
    & $python -m app.cli run-worker --once --cli-token $env:WIN_AGENT_CLI_TOKEN
    exit $LASTEXITCODE
}

& $python -m app.cli run-worker --limit $Limit --interval $Interval --cli-token $env:WIN_AGENT_CLI_TOKEN
