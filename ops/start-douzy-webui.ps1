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
  # 8518 已经有人监听（多半是 启动工具箱.vbs / .bat 起的 pythonw 实例）时不要再起一个：
  # 新实例抢不到端口会立刻退出，守护循环就变成每 10 秒写一轮「starting / exited」的
  # 空转，把日志刷满，还让人误以为服务在反复崩溃。
  $listening = Get-NetTCPConnection -LocalPort 8518 -State Listen -ErrorAction SilentlyContinue
  if ($listening) {
    Start-Sleep -Seconds 15
    continue
  }
  Add-Content $log ('[{0}] server starting' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
  & $py $server *>> $log
  Add-Content $log ('[{0}] server exited, restart in 10s' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
  Start-Sleep -Seconds 10
}
