param(
    [string]$PythonPath = (Get-Command python.exe).Source,
    [int]$Port = 8766,
    [ValidateRange(0, 4000)][int]$TrackLimit = 0,
    [switch]$ImportArchive,
    [switch]$CatchUp
)

$ErrorActionPreference = 'Stop'
if ($ImportArchive -and $TrackLimit) { throw '历史轨迹导入须和 17TRACK 查询分开运行' }
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = $PSScriptRoot
if ($CatchUp) {
    if (-not $TrackLimit) { throw '追赶模式需要指定 17TRACK 批量' }
    Push-Location -LiteralPath $root
    try {
        function Read-OverdueAttempts {
            $count = & $PythonPath -c "from app.tracking_freshness import coverage; print(coverage()['overdueAttempts'])"
            if ($LASTEXITCODE -ne 0 -or $count -notmatch '^\d+$') { throw '读取待核对运单失败' }
            return [int]$count
        }

        $remaining = Read-OverdueAttempts
        $stalled = 0
        for ($batch = 0; $batch -lt 6; $batch++) {
            & $PSCommandPath -PythonPath $PythonPath -Port $Port -TrackLimit $TrackLimit
            if (-not $remaining) { break }
            $next = Read-OverdueAttempts
            if (-not $next) { break }
            if ($next -ge $remaining) {
                $stalled++
                if ($stalled -ge 2) { break }
                Start-Sleep -Seconds 10
            } else {
                $stalled = 0
            }
            $remaining = $next
        }
    } finally {
        Pop-Location
    }
    return
}
$facts = Join-Path $root '履约单.duckdb'
$initialData = Join-Path $env:USERPROFILE 'Desktop\履约单和轨迹分析2026'
$log = Join-Path $root 'rolling-sync.log'
$stage = Join-Path $root ('.rolling-' + [guid]::NewGuid().ToString('N') + '.duckdb')
$manifest = [System.IO.Path]::ChangeExtension($stage, '.json')
$mutex = [System.Threading.Mutex]::new($false, 'Local\FulfillmentControlTowerRolling')
$importPriority = [System.Threading.Mutex]::new($false, 'Local\FulfillmentControlTowerImportPriority')
$ownsLock = $false
$ownsPriority = $false
$previewStopped = $false
$runId = $null
$failure = 'prepare'

function Enter-SyncMutex([System.Threading.Mutex]$Mutex, [int]$TimeoutMilliseconds) {
    try { return $Mutex.WaitOne($TimeoutMilliseconds) }
    catch [System.Threading.AbandonedMutexException] { return $true }
}

Push-Location -LiteralPath $root
try {
    $isErpImport = -not $TrackLimit -and -not $ImportArchive
    $wait = [int][TimeSpan]::FromMinutes(15).TotalMilliseconds
    if (-not $ImportArchive) {
        # Keep new tracking batches behind any ERP import waiting for the database.
        $ownsPriority = Enter-SyncMutex $importPriority $wait
        if (-not $ownsPriority) {
            throw '等待履约单导入超时，下次任务继续'
        }
    }
    $ownsLock = Enter-SyncMutex $mutex 0
    if ($isErpImport -and -not $ownsLock) {
        Add-Content -LiteralPath $log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 履约单更新排队，等待当前写入完成" -Encoding utf8
        $ownsLock = Enter-SyncMutex $mutex $wait
        if (-not $ownsLock) {
            throw '等待当前数据更新超时，履约单将在下次检查重试'
        }
    }
    if ($TrackLimit -and $ownsPriority) {
        $importPriority.ReleaseMutex()
        $ownsPriority = $false
    }
    if (-not $ownsLock) {
        if ($ImportArchive) { throw '另一个数据更新任务正在运行，请稍后再导入历史轨迹' }
        return
    }

    $started = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $job = if ($ImportArchive) { '17TRACK 历史轨迹导入' } elseif ($TrackLimit) { '17TRACK' } else { '履约单 Excel 与 Webhook' }
    Add-Content -LiteralPath $log -Value "[$started] 检查 $job 更新" -Encoding utf8
    if ($TrackLimit) {
        $runId = & $PythonPath -u rolling_update.py poll-start
        if ($LASTEXITCODE -ne 0 -or $runId -notmatch '^\d+$') { throw '无法记录 17TRACK 任务启动' }
    }
    $prepareArgs = @('-u', 'rolling_update.py', 'prepare', '--stage', $stage)
    if ($ImportArchive) { $prepareArgs += '--import-archive' }
    $previousToken = $env:TRACK17_TOKEN
    try {
        if ($TrackLimit) {
            $failure = 'credentials'
            if (-not $env:TRACK17_TOKEN) {
                $credentialPath = Join-Path $env:LOCALAPPDATA 'FulfillmentControlTower\track17.dpapi'
                $protectedToken = (Get-Content -LiteralPath $credentialPath -Raw).Trim() | ConvertTo-SecureString
                $env:TRACK17_TOKEN = [pscredential]::new('17TRACK', $protectedToken).GetNetworkCredential().Password
            }
            $prepareArgs += @('--track-limit', $TrackLimit)
        }
        $failure = 'prepare'
        & $PythonPath @prepareArgs 2>&1 | ForEach-Object {
            Add-Content -LiteralPath $log -Value ([string]$_) -Encoding utf8
        }
        $prepareExit = $LASTEXITCODE
    } finally {
        $env:TRACK17_TOKEN = $previousToken
    }
    if ($prepareExit -eq 3) {
        if ($runId) { & $PythonPath -u rolling_update.py poll-idle --run-id $runId }
        return
    }
    if ($prepareExit -ne 0) { throw '履约监控系统生成更新快照失败' }
    $failure = 'publish'

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $command = $owner.CommandLine.Replace('\','/')
        if ($owner.Name -ne 'python.exe' -or $command -notmatch 'preview\.py\s+' -or
            $command -notlike ('*' + $facts.Replace('\','/') + '*')) {
            throw '端口由其他程序使用，更新将在下一次重试'
        }
        Stop-Process -Id $owner.ProcessId
        $previewStopped = $true
        Wait-Process -Id $owner.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    $publishArgs = @('-u', 'rolling_update.py', 'publish', '--stage', $stage)
    if ($runId) { $publishArgs += @('--run-id', $runId) }
    & $PythonPath @publishArgs 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $log -Value ([string]$_) -Encoding utf8
    }
    if ($LASTEXITCODE -ne 0) { throw '履约监控系统快照切换失败' }
} catch {
    Add-Content -LiteralPath $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $_.Exception.Message) -Encoding utf8
    if ($runId -match '^\d+$') {
        & $PythonPath -u rolling_update.py poll-failed --run-id $runId --reason $failure
    }
    throw
} finally {
    try {
        if ($previewStopped) {
            $previewArgs = '-u preview.py --data-dir "{0}" --facts-file "{1}" --port {2}' -f $initialData,$facts,$Port
            Start-Process -FilePath $PythonPath -ArgumentList $previewArgs -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'rolling-sync-preview-out.log') -RedirectStandardError (Join-Path $root 'rolling-sync-preview-err.log')
        }
    } finally {
        foreach ($path in @($stage, $manifest, "$stage.wal")) {
            if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path }
        }
        if ($ownsLock) { $mutex.ReleaseMutex() }
        if ($ownsPriority) { $importPriority.ReleaseMutex() }
        $importPriority.Dispose()
        $mutex.Dispose()
        Pop-Location
    }
}
