@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
set "START_SCRIPT=%PROJECT_ROOT%start_app.cmd"

if not exist "%START_SCRIPT%" (
  echo Start script was not found:
  echo   "%START_SCRIPT%"
  pause
  exit /b 1
)

echo Restarting My Portfolio Bets V3 web app...
call "%START_SCRIPT%"
exit /b %errorlevel%
