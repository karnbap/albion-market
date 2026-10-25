$ErrorActionPreference = 'Stop'
$albionRoot = $PSScriptRoot
New-Item -ItemType Directory -Force -Path (Join-Path $albionRoot 'data') | Out-Null
# Keep a manual stop effective until the next game session.
Set-Content -LiteralPath (Join-Path $albionRoot 'data\game-watch-paused') -Value 'Manual stop; resume after game exits.'
$albionTargets = @(@{File='capture.pid'; Expected=(Join-Path $albionRoot 'vendor\albiondata-client.exe')}, @{File='server.pid'; Expected='market.py'})
foreach ($albionTarget in $albionTargets) {
    $albionPidPath = Join-Path $albionRoot ('data\' + $albionTarget.File)
    if (-not (Test-Path -LiteralPath $albionPidPath)) { continue }
    $albionProcessId = [int](Get-Content -LiteralPath $albionPidPath)
    $albionInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$albionProcessId"
    if (-not $albionInfo) { continue }
    if ($albionTarget.File -eq 'capture.pid') {
        if ($albionInfo.ExecutablePath -eq $albionTarget.Expected) { Stop-Process -Id $albionProcessId }
    } elseif ($albionInfo.CommandLine -like "*$albionRoot\market.py*") { Stop-Process -Id $albionProcessId }
}
