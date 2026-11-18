@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"
set "APP_FILE=%PROJECT_ROOT%app.py"

if not exist "%PYTHON_EXE%" (
  echo Python virtual environment was not found:
  echo   "%PYTHON_EXE%"
  echo.
  echo Create it with:
  echo   py -m venv .venv
  echo   .venv\Scripts\pip.exe install -r requirements.txt
  echo.
  pause
  exit /b 1
)

cd /d "%PROJECT_ROOT%"
echo Checking for an existing My Portfolio Bets V3 web app...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$project = '%PROJECT_ROOT%'.TrimEnd('\'); $matches = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine -match 'app\.py' -and $_.CommandLine.IndexOf($project, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 }); foreach ($process in $matches) { Stop-Process -Id $process.ProcessId -Force }; if ($matches.Count -gt 0) { Start-Sleep -Milliseconds 500 }"
if errorlevel 1 (
  echo Could not check or stop the existing web app.
  pause
  exit /b 1
)

echo Starting My Portfolio Bets V3 web app...
echo URL: http://127.0.0.1:5002/
echo.
"%PYTHON_EXE%" "%APP_FILE%"

echo.
echo App stopped.
pause
