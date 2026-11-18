param(
    [int]$KeepDays = 14
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python virtual environment not found: $PythonExe"
}

& $PythonExe -c "from services.report_cleanup import cleanup_old_reports; result = cleanup_old_reports(keep_days=$KeepDays); print(f\"Removed {result['removed']} report file(s). Kept {result['kept']}.\")"
