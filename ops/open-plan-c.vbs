' Douzy desktop launcher: ensure resident service, then open the MERGED app home (/ = planc-home workbench)
' ASCII-only on purpose. Single entry - the merged interactive app, not the static mockups.
Dim shell
Set shell = CreateObject("WScript.Shell")

' 1) Ensure service: TCP check on 8518; if down, start scheduled task and wait
Dim ensure
ensure = "powershell -NoProfile -WindowStyle Hidden -Command " & _
  """$c = New-Object Net.Sockets.TcpClient; " & _
  "if (-not $c.ConnectAsync('127.0.0.1', 8518).Wait(2000)) { " & _
  "Start-ScheduledTask -TaskName DouzyWebUI; Start-Sleep -Seconds 6 } """
shell.Run ensure, 0, True

' 2) Open the merged interactive app home
shell.Run "http://127.0.0.1:8518/"
