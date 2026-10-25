# Run by the user/ordinary launcher; restarts only this app's receiver.
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$albionRoot=$PSScriptRoot
try {
    $albionPidPath=Join-Path $albionRoot 'data\server.pid'
    if(Test-Path -LiteralPath $albionPidPath) {
        $albionServerId=[int](Get-Content -LiteralPath $albionPidPath)
        $albionServerInfo=Get-CimInstance Win32_Process -Filter "ProcessId=$albionServerId"
        if($albionServerInfo) {
            if($albionServerInfo.CommandLine -notlike "*$albionRoot\market.py*") {throw 'The PID belongs to another program; nothing was stopped.'}
            $albionOldProcess=Get-Process -Id $albionServerId
            Stop-Process -Id $albionServerId
            $albionOldProcess.WaitForExit(5000) | Out-Null
        }
    }
    $albionExisting=$false
    $albionRestartConfig=Get-Content -LiteralPath (Join-Path $albionRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    try {
        $albionRestartHealth=Invoke-RestMethod -Uri "http://127.0.0.1:$($albionRestartConfig.port)/api/health" -TimeoutSec 2
        $albionExisting=$albionRestartHealth.app -eq 'AlbionLocal'
    } catch { }
    if($albionExisting) { throw 'An older receiver is still active without a matching owned PID. Close it before launching again.' }
    & (Join-Path $albionRoot 'start.ps1') -DashboardOnly -NoBrowser:$NoBrowser
} catch {
    if($NoBrowser) { throw }
    Write-Host $_.Exception.Message
    Read-Host 'Press Enter to close'
}
