param(
    [Parameter(Mandatory)][int]$RunId,
    [string]$PythonPath = (Get-Command python.exe).Source
)

$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'rolling-sync.log'
$mutex = [System.Threading.Mutex]::new($false, 'Local\FulfillmentControlTowerRolling')
$locked = $false
$previousToken = $env:TRACK17_TOKEN
Push-Location -LiteralPath $PSScriptRoot
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { exit 4 }

    if (-not $env:TRACK17_TOKEN) {
        try {
            $credentialPath = Join-Path $env:LOCALAPPDATA 'FulfillmentControlTower\track17.dpapi'
            $protected = (Get-Content -LiteralPath $credentialPath -Raw).Trim() | ConvertTo-SecureString
            $env:TRACK17_TOKEN = [pscredential]::new('17TRACK', $protected).GetNetworkCredential().Password
        } catch {
            Add-Content -LiteralPath $log -Encoding utf8 -Value '17TRACK full pull: credentials unavailable'
            exit 5
        }
    }
    $env:PYTHONUTF8 = '1'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    & $PythonPath -u rolling_update.py full-batch --run-id $RunId 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $log -Encoding utf8 -Value ([string]$_)
    }
    exit $LASTEXITCODE
} finally {
    $env:TRACK17_TOKEN = $previousToken
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    Pop-Location
}
