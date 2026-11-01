@echo off
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8080
py -3 server.py --host 0.0.0.0 --port %PORT%
pause
