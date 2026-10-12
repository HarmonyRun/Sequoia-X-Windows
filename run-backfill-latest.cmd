@echo off
REM ============================================================
REM  Sequoia-X  post-close incremental backfill
REM
REM  Fills the latest trading day for the whole market, one hour
REM  after the A-share close (scheduled Mon-Fri 16:00).
REM
REM  Data source comes from .env: DATA_SOURCE=auto tries
REM  eastmoney -> tencent -> sina and automatically switches when
REM  one of them is rate-limited or unreachable.
REM
REM  Safe to interrupt: re-running resumes where it left off.
REM  Logs to logs\backfill-YYYYMMDD.log
REM
REM  Usage:  run-backfill-latest.cmd             (pauses at end)
REM          run-backfill-latest.cmd --no-pause  (Task Scheduler)
REM
REM  NOTE: keep this file pure ASCII (see run-daily.cmd for why).
REM ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0Sequoia-X"

if not exist "logs" mkdir logs

REM On Chinese Windows %date% is "<weekday> YYYY/MM/DD" (weekday first),
REM so take the trailing YYYY/MM/DD instead of the leading tokens.
set D=%date:~-10,4%%date:~-5,2%%date:~-2%
if "%D:~7,1%"=="" set D=undated

echo [Sequoia-X] post-close backfill at %date% %time% >> "logs\backfill-%D%.log"
echo [Sequoia-X] running post-close backfill at %date% %time%
".venv\Scripts\python.exe" main.py --backfill >> "logs\backfill-%D%.log" 2>&1
set RC=%ERRORLEVEL%
echo [Sequoia-X] exit code = %RC%
echo [Sequoia-X] log: %CD%\logs\backfill-%D%.log

if "%~1"=="--no-pause" exit /b %RC%
pause
