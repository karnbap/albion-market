param([switch]$InstallOnly)
$ErrorActionPreference='Stop'
$albionWatchRoot=$PSScriptRoot
$albionWatchPythonOnPath=Get-Command pythonw.exe -ErrorAction SilentlyContinue
$albionWatchConsolePython=Get-Command python.exe -ErrorAction SilentlyContinue
$albionWatchCandidates=@()
if($env:ALBION_PYTHON) { $albionWatchCandidates+=Join-Path (Split-Path $env:ALBION_PYTHON) 'pythonw.exe' }
$albionWatchCandidates+='C:\Python314\pythonw.exe'
if($albionWatchPythonOnPath) { $albionWatchCandidates+=$albionWatchPythonOnPath.Source }
if($albionWatchConsolePython) { $albionWatchCandidates+=Join-Path (Split-Path $albionWatchConsolePython.Source) 'pythonw.exe' }
$albionWatchPython=$albionWatchCandidates | Where-Object {$_ -and (Test-Path -LiteralPath $_)} | Select-Object -First 1
if(-not $albionWatchPython) { throw 'Pythonw is missing.' }
$albionWatchScript=Join-Path $albionWatchRoot 'game_watch.py'
if(-not (Test-Path -LiteralPath $albionWatchScript)) { throw 'Game watcher is missing.' }
$albionWatchStartup=[Environment]::GetFolderPath('Startup')
$albionWatchLink=Join-Path $albionWatchStartup 'AlbionMarket-GameWatch.lnk'
$albionWatchShell=New-Object -ComObject WScript.Shell
$albionWatchShortcut=$albionWatchShell.CreateShortcut($albionWatchLink)
$albionWatchShortcut.TargetPath=$albionWatchPython
$albionWatchShortcut.Arguments='"'+$albionWatchScript+'"'
$albionWatchShortcut.WorkingDirectory=$albionWatchRoot
$albionWatchShortcut.WindowStyle=7
$albionWatchShortcut.Description='Start local market capture with Albion; stop capture after the game exits.'
$albionWatchShortcut.Save()
if(-not $InstallOnly) {
    Start-Process -FilePath $albionWatchPython -ArgumentList @('"'+$albionWatchScript+'"') -WorkingDirectory $albionWatchRoot -WindowStyle Hidden
}
Write-Output ('Game lifecycle integration installed: '+$albionWatchLink)
