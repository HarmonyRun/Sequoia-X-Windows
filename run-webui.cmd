@echo off
REM ============================================================
REM  Sequoia-X  Web workbench launcher
REM
REM  Opens a local dashboard at http://127.0.0.1:8899
REM  (read-only view of the database, plus buttons to run
REM   screening / signal-only / full backfill in the background).
REM
REM  Bound to 127.0.0.1 only - not reachable from other machines.
REM  Change the port in .env:  WEBUI_PORT=8899
REM
REM  Usage:  run-webui.cmd              (keeps this window open)
REM          run-webui.cmd --no-browser (do not open a browser)
REM
REM  NOTE: keep this file pure ASCII (see run-daily.cmd for why).
REM ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM Work out where the app actually lives. These launchers work both
REM from the repo root and from the parent folder that holds the clone.
set "APPDIR=%~dp0"
if exist "%~dp0Sequoia-X\main.py" set "APPDIR=%~dp0Sequoia-X"
cd /d "%APPDIR%"

if not exist "logs" mkdir logs

REM Port: take it from .env if present, otherwise 8899
set WEBUI_PORT=8899
for /f "tokens=1,* delims==" %%a in ('findstr /b /i "WEBUI_PORT" .env 2^>nul') do set WEBUI_PORT=%%b
set WEBUI_PORT=%WEBUI_PORT: =%

echo [Sequoia-X] Web workbench starting on http://127.0.0.1:%WEBUI_PORT%
echo [Sequoia-X] press Ctrl+C in this window to stop

if /i "%~1"=="--no-browser" goto run
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%WEBUI_PORT%"

:run
".venv\Scripts\python.exe" -m webui.server
echo [Sequoia-X] workbench stopped, exit code = %ERRORLEVEL%
pause
