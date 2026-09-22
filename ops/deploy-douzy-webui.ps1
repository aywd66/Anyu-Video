# Douzy WebUI resident deployment manager (ASCII junction C:\douzy)
# usage: -Install | -Remove | -Restart | -Stop | -Status
param(
  [switch]$Install,
  [switch]$Remove,
  [switch]$Restart,
  [switch]$Stop,
  [switch]$Status
)

$TaskName = "DouzyWebUI"
$Port = 8518
$Wrapper = "C:\douzy\ops\start-douzy-webui.ps1"

if ($Status) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Output "NOT-INSTALLED"; exit 0 }
  $info = Get-ScheduledTaskInfo -TaskName $TaskName
  $listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue)
  Write-Output ("state={0} lastRun={1} lastResult={2} port{3}={4}" -f $t.State, $info.LastRunTime, $info.LastTaskResult, $Port, $(if ($listening) {"LISTENING"} else {"DOWN"}))
  exit 0
}

if ($Stop) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bridge_server.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*start-douzy-webui.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Write-Output "STOPPED"
  exit 0
}

if ($Restart) {
  & $PSCommandPath -Stop | Out-Null
  Start-Sleep -Seconds 2
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 6
  & $PSCommandPath -Status
  exit 0
}

if ($Remove) {
  & $PSCommandPath -Stop | Out-Null
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "REMOVED (junction C:\douzy kept; remove with: rmdir C:\douzy)"
  exit 0
}

if ($Install) {
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Wrapper`""
  $boot = New-ScheduledTaskTrigger -AtStartup
  $daily = New-ScheduledTaskTrigger -Daily -At 09:00
  $settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $boot, $daily -Settings $settings `
    -Description "Douzy WebUI (bridge 8518) resident service via C:\douzy junction" -Force | Out-Null

  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 6
  Write-Output "INSTALLED & STARTED"
  & $PSCommandPath -Status
  exit 0
}

Write-Output "usage: -Install | -Remove | -Restart | -Stop | -Status"
