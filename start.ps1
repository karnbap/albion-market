param([switch]$DashboardOnly, [switch]$NoBrowser, [switch]$Automatic)
$ErrorActionPreference = 'Stop'
$albionRoot = $PSScriptRoot
# A fresh clone has no data folder yet; redirected logs and PID files need it.
New-Item -ItemType Directory -Force -Path (Join-Path $albionRoot 'data') | Out-Null
$albionPythonOnPath = Get-Command python.exe -ErrorAction SilentlyContinue
$albionPythonCandidates = @($env:ALBION_PYTHON, 'C:\Python314\python.exe', (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'), $albionPythonOnPath.Source)
$albionPython = $albionPythonCandidates | Where-Object {$_ -and (Test-Path -LiteralPath $_)} | Select-Object -First 1
if (-not $albionPython) { throw 'Python is missing. Install Python 3.11+ or edit start.ps1.' }
$albionConfig = Get-Content -LiteralPath (Join-Path $albionRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$albionPort = [int]$albionConfig.port
$albionAddress = "http://127.0.0.1:$albionPort"
$albionRunning = $false
try {
    $albionHealth = Invoke-RestMethod -Uri "$albionAddress/api/health" -TimeoutSec 2
    if ($albionHealth.app -eq 'AlbionLocal') { $albionRunning = $true }
    else { throw 'The configured port belongs to another application.' }
} catch { if ($_.Exception.Message -like '*another application*') { throw } }
if ($albionRunning -and -not $albionHealth.auto_update.enabled -and -not $Automatic) {
    # User launched the ordinary app shortcut: migrate the old receiver once.
    # Later code/asset updates are applied in-process without this restart.
    & (Join-Path $albionRoot 'restart-analysis.ps1') -NoBrowser
    $albionHealth = Invoke-RestMethod -Uri "$albionAddress/api/health" -TimeoutSec 5
    if (-not $albionHealth.auto_update.enabled) { throw 'Automatic update setup did not complete. Check data\server-stderr.log.' }
}
if (-not $albionRunning) {
    $albionPidPath = Join-Path $albionRoot 'data\server.pid'
    $albionPreviousPid = if (Test-Path -LiteralPath $albionPidPath) { (Get-Content -LiteralPath $albionPidPath -Raw).Trim() } else { $null }
    $albionProcess = Start-Process -FilePath $albionPython -ArgumentList @('"' + (Join-Path $albionRoot 'market.py') + '"') -WorkingDirectory $albionRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $albionRoot 'data\server-stdout.log') -RedirectStandardError (Join-Path $albionRoot 'data\server-stderr.log')
    Set-Content -LiteralPath $albionPidPath -Value $albionProcess.Id
    for ($albionAttempt=0; $albionAttempt -lt 75; $albionAttempt++) {
        Start-Sleep -Milliseconds 200
        try { $albionHealth = Invoke-RestMethod -Uri "$albionAddress/api/health" -TimeoutSec 1; if ($albionHealth.app -eq 'AlbionLocal') { $albionRunning=$true; break } } catch { }
    }
    # A duplicate start that lost the port to a still-running receiver exits at once;
    # keep that receiver's PID so stop.ps1 can still find it.
    if ($albionProcess.HasExited -and $albionPreviousPid) { Set-Content -LiteralPath $albionPidPath -Value $albionPreviousPid }
    if (-not $albionRunning) { throw 'Receiver did not start. Check data\server-stderr.log.' }
}
if (-not $NoBrowser) { Start-Process $albionAddress }
if ($DashboardOnly) { exit }
if(-not $Automatic) {
    $albionPausePath=Join-Path $albionRoot 'data\game-watch-paused'
    if(Test-Path -LiteralPath $albionPausePath) { Remove-Item -LiteralPath $albionPausePath }
}
$albionCaptureMutex=[System.Threading.Mutex]::new($false,'Local\AlbionMarketCaptureStart')
$albionCaptureLocked=$false
try {
    try { $albionCaptureLocked=$albionCaptureMutex.WaitOne(30000) } catch [System.Threading.AbandonedMutexException] { $albionCaptureLocked=$true }
    if(-not $albionCaptureLocked) { throw 'Capture launcher is busy.' }
$albionCapture = Join-Path $albionRoot 'vendor\albiondata-client.exe'
# The local fork (README: 전용 수집기) reads sale/buy mails and the in-game "my orders" lists
# itself and sends them only to the private localhost URL. Use it whenever it is present.
$albionFork = Join-Path $albionRoot 'vendor\albiondata-client-fork.exe'
if (Test-Path -LiteralPath $albionFork) { $albionCapture = $albionFork }
if (-not (Test-Path -LiteralPath $albionCapture)) { if($Automatic) { throw 'Capture client missing; run setup manually.' }; & $albionPython (Join-Path $albionRoot 'setup.py') }
$albionOtherClients = Get-Process -Name 'albiondata-client','albiondata-client-fork' -ErrorAction SilentlyContinue
if ($albionOtherClients) {
    Write-Host 'An Albion Data Client is already running. It was not stopped or replaced.'
    Write-Host 'Close that client yourself, then run this launcher again to use local capture.'
    if(-not $Automatic) { Read-Host 'Press Enter to close this window' }
    exit
}
$albionDriver = Get-Service -Name 'npcap','npf' -ErrorAction SilentlyContinue
if (-not $albionDriver) {
    if($Automatic) { throw 'Npcap is required. Automatic mode does not install software.' }
    Write-Host 'Npcap is required for automatic capture and is not installed.'
    Write-Host 'Official download: https://npcap.com/#download'
    Write-Host 'During setup, enable: Install Npcap in WinPcap API-compatible Mode.'
    Write-Host 'After installation, run this launcher again. The dashboard is ready.'
    Start-Process 'https://npcap.com/#download'
    Read-Host 'Press Enter to close this window'
    exit
}
# Public orders and private sale-mail summaries stay on this computer.
# Private uploads include public packets too; the receiver ignores those copies.
# Version 0.1.61 crashes with 'noop'; private uploads use a full localhost HTTP URL.
# Do NOT add -d: that disables local HTTP ingestion too.
# -debug with an operation whitelist writes the in-game 'my orders' requests and mailbox messages
# to vendor\albiondata-client.log; the receiver reads them (CAPTURE_DEBUG_OPERATIONS in market.py).
# The fork sends both itself; the log then only feeds the dashboard's raw-record view.
# -events 65535 matches no event, so game events stay out of the log.
$albionClientProcess = Start-Process -FilePath $albionCapture -ArgumentList @('-i', "$albionAddress/ingest/europe", '-p', "$albionAddress/private/europe", '-minimize', '-debug', '-operations', '81,82,94,174,175,176', '-events', '65535') -WorkingDirectory (Join-Path $albionRoot 'vendor') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $albionRoot 'data\capture-stdout.log') -RedirectStandardError (Join-Path $albionRoot 'data\capture-stderr.log')
Set-Content -LiteralPath (Join-Path $albionRoot 'data\capture.pid') -Value $albionClientProcess.Id
Write-Host 'Local capture started. Open Albion Europe and load a market item.'

} finally {
    if($albionCaptureLocked) { $albionCaptureMutex.ReleaseMutex() }
    $albionCaptureMutex.Dispose()
}
