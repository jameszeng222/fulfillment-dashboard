param(
    [Parameter(Mandatory)][ValidateSet('Origin','Tunnel','Login','Snapshot')][string]$Component,
    [string]$PythonPath = (Get-Command python.exe).Source
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$state = Join-Path $env:LOCALAPPDATA 'FulfillmentMonitorCloud'

function Read-ProtectedValue([string]$Name) {
    $sealed = Get-Content -LiteralPath (Join-Path $state $Name) -Raw | ConvertTo-SecureString
    [Net.NetworkCredential]::new('', $sealed).Password
}

Push-Location -LiteralPath $root
try {
    if ($Component -eq 'Tunnel') {
        $env:TUNNEL_TOKEN = Read-ProtectedValue 'cloudflared.dpapi'
        & (Join-Path $state 'cloudflared.exe') tunnel --no-autoupdate run
        exit $LASTEXITCODE
    }

    $env:PYTHONUTF8 = '1'
    $env:OPS_SITE_ORIGIN = 'https://fulfillment-monitor.pages.dev'
    if ($Component -eq 'Login') {
        Start-Process -FilePath $env:OPS_SITE_ORIGIN
        exit 0
    }
    $env:OPS_LOCAL_LOGIN = 'false'
    $env:FULFILLMENT_PUBLIC_PREVIEW = 'true'
    $env:FULFILLMENT_CLOUD_TICKETS = 'true'
    $env:FULFILLMENT_ONLINE_USERS = 'fulfillment-online'
    $env:FULFILLMENT_EDGE_KEY = Read-ProtectedValue 'edge-key.dpapi'
    if ($Component -eq 'Snapshot') {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        & $PythonPath -u -m cloudflare.publish_snapshot *>> (Join-Path $state 'snapshot-sync.log')
        exit $LASTEXITCODE
    }
    $env:FULFILLMENT_OPEN_TEST = 'true'
    & $PythonPath -m uvicorn app.cloudflare_origin:application --host 127.0.0.1 --port 8770 --lifespan on --no-access-log
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
