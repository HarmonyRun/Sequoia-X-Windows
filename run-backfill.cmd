@echo off
REM ============================================================
REM  Sequoia-X  first-time / gap-fill history backfill
REM  Official single-threaded mode: robust, but ~1.5-2 hours
REM  for the whole market on this machine.
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
echo [Sequoia-X] backfill (official mode) starting at %date% %time%
echo [Sequoia-X] this may take 1-2 hours; safe to interrupt and re-run (it resumes)
".venv\Scripts\python.exe" main.py --backfill
echo [Sequoia-X] exit code = %ERRORLEVEL%
pause
