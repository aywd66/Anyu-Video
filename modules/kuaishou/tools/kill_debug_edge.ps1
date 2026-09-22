$procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'"
$targets = $procs | Where-Object { $_.CommandLine -like '*edge-manual-profile*' }
foreach ($t in $targets) {
    try { Stop-Process -Id $t.ProcessId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -like '*edge-manual-profile*' }
Write-Output ("DEBUG_EDGE_LEFT=" + (@($left).Count))
$user = Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle }
Write-Output "USER_WINDOWS_ALIVE:"
foreach ($u in $user) { Write-Output ("  " + $u.Id + " " + $u.MainWindowTitle) }
