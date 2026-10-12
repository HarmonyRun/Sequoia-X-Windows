@echo off
REM ============================================================
REM  Sequoia-X  run the project's own test suite (pytest)
REM ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0Sequoia-X"
".venv\Scripts\python.exe" -m pytest -q
pause
