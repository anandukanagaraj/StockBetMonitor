param(
    [int]$Keep = 30
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DbPath = Join-Path $ProjectRoot "data\stock_monitor.db"
$BackupDir = Join-Path $ProjectRoot "data\backups"

if ($Keep -lt 1) {
    throw "Keep must be at least 1."
}

if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "Database not found: $DbPath"
}

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupPath = Join-Path $BackupDir "stock_monitor_$Timestamp.db"

Copy-Item -LiteralPath $DbPath -Destination $BackupPath -Force

$Backups = Get-ChildItem -LiteralPath $BackupDir -Filter "stock_monitor_*.db" |
    Sort-Object LastWriteTime -Descending

$Backups |
    Select-Object -Skip $Keep |
    Remove-Item -Force

Write-Host "Database backup created:"
Write-Host $BackupPath
Write-Host "Keeping latest $Keep backup(s) in $BackupDir"
