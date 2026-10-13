@echo off
REM ============================================================
REM  Sequoia-X  run the project's own test suite (pytest)
REM ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM Work out where the app actually lives. These launchers work both
REM from the repo root and from the parent folder that holds the clone.
set "APPDIR=%~dp0"
if exist "%~dp0Sequoia-X\main.py" set "APPDIR=%~dp0Sequoia-X"
cd /d "%APPDIR%"
".venv\Scripts\python.exe" -m pytest -q
pause
