@echo off
rem Opens the discovery dashboard: starts its local server if it is not running, then the page.
cd /d "%~dp0.."
powershell -NoProfile -WindowStyle Hidden -Command "if (-not (Get-NetTCPConnection -LocalPort 8798 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -FilePath pythonw -ArgumentList 'dashboard\server.py' -WorkingDirectory '%~dp0..' }"
timeout /t 2 /nobreak >nul
start "" http://localhost:8798
