param(
    [int]$Keep = 30,
    [string]$CopyDir = ""
)

# Creates a consistent database backup using SQLite's backup API (includes
# changes still in the WAL file). The scheduler also runs this daily after
# REPORT_TIME; see the [BACKUP] section in config.ini.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($Keep -lt 1) {
    throw "Keep must be at least 1."
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python executable not found: $PythonPath"
}

$Arguments = @("-m", "services.db_backup", "--keep", $Keep)
if ($CopyDir) {
    $Arguments += @("--copy-dir", $CopyDir)
}

Push-Location $ProjectRoot
try {
    & $PythonPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Database backup failed."
    }
}
finally {
    Pop-Location
}
