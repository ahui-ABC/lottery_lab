@echo off
REM 停止开机自启的竞彩赔率采集守护进程。
REM 只结束命令行里带 collect-odds 的 pythonw 进程，不影响其他 Python 程序。

powershell -NoProfile -Command ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*collect-odds*' };" ^
  "if (-not $procs) { Write-Host '没有在运行的赔率采集进程。'; exit 0 };" ^
  "foreach ($p in $procs) { Write-Host ('结束 PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force }"

pause
