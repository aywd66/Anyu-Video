# Douzy WebUI wrapper (ASCII junction path C:\douzy): log rotate + daemon loop + crash auto-restart
$ErrorActionPreference = 'SilentlyContinue'

$root    = 'C:\douzy'
$logDir  = Join-Path $root 'logs'
$log     = Join-Path $logDir 'douzy-webui.log'
$py      = Join-Path $root 'runtime\douyin-venv\Scripts\python.exe'
$server  = Join-Path $root 'modules\douyin\webui\bridge_server.py'
$workdir = Join-Path $root 'modules\douyin'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) { Move-Item $log ($log + '.1') -Force }

Add-Content $log ('[{0}] watchdog start (daemon loop)' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Set-Location $workdir
while ($true) {
  Add-Content $log ('[{0}] server starting' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
  & $py $server *>> $log
  Add-Content $log ('[{0}] server exited, restart in 10s' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
  Start-Sleep -Seconds 10
}
