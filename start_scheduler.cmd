@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "SCHEDULER_FILE=%PROJECT_ROOT%scheduler.py"

if not exist "%PYTHON_EXE%" (
  echo Python virtual environment was not found:
  echo   "%PYTHON_EXE%"
  pause
  exit /b 1
)

cd /d "%PROJECT_ROOT%"
echo Starting My Portfolio Bets V3 scheduler...
echo Press Ctrl+C to stop.
echo.
"%PYTHON_EXE%" "%SCHEDULER_FILE%"
