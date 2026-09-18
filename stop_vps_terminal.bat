@echo off
REM stop_vps_terminal.bat - Stop running GovAward terminal local tunnel watchdog
cd /d "%~dp0"
echo =======================================================
echo Stopping GovAward Alpha Local Tunnel Watchdog...
echo =======================================================

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tunnel_vps_terminal.py*' -or ($_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*8055:127.0.0.1:8055*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('  • Terminated ' + $_.Name + ' (PID ' + $_.ProcessId + ')') }"

if exist "data\tunnel.pid" del /f /q "data\tunnel.pid" >nul 2>&1

echo [OK] All local tunnel processes stopped.
timeout /t 3
