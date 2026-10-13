@echo off
REM ============================================================
REM  Sequoia-X  daily stock screening  (run after market close)
REM  Reads .env for the Feishu webhook.
REM  Logs to logs\daily-YYYYMMDD.log
REM
REM  Usage:  run-daily.cmd             (interactive, pauses at end)
REM          run-daily.cmd --no-pause  (for Task Scheduler)
REM
REM  NOTE: keep this file pure ASCII. cmd.exe mis-tracks byte offsets
REM  when a batch file mixes "chcp 65001" with non-ASCII characters,
REM  which makes it re-read earlier lines in an endless loop.
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

REM On Chinese Windows %date% is "<weekday> YYYY/MM/DD" (weekday first),
REM so take the trailing YYYY/MM/DD instead of the leading tokens.
set D=%date:~-10,4%%date:~-5,2%%date:~-2%
if "%D:~7,1%"=="" set D=undated

echo [Sequoia-X] daily run at %date% %time% >> "logs\daily-%D%.log"
echo [Sequoia-X] running daily mode at %date% %time%
".venv\Scripts\python.exe" main.py >> "logs\daily-%D%.log" 2>&1
set RC=%ERRORLEVEL%
echo [Sequoia-X] exit code = %RC%
echo [Sequoia-X] log: %CD%\logs\daily-%D%.log

if "%~1"=="--no-pause" exit /b %RC%
pause
