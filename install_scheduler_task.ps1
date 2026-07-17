param(
    [string]$TaskName = "StockMonitorV3Scheduler"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$SchedulerPath = Join-Path $ProjectRoot "scheduler.py"

if (-not (Test-Path $PythonPath)) {
    throw "Python executable not found: $PythonPath"
}

if (-not (Test-Path $SchedulerPath)) {
    throw "Scheduler script not found: $SchedulerPath"
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$SchedulerPath`"" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

$Description = "Runs StockMonitor V3 scheduler for market checks, WhatsApp alerts, EOD email, and report publishing."

function Install-StartupLauncher {
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $LauncherPath = Join-Path $StartupDir "$TaskName.cmd"

    $Lines = @(
        "@echo off",
        "REM Starts StockMonitor V3 scheduler at Windows login.",
        "cd /d `"$ProjectRoot`"",
        "`"$PythonPath`" `"$SchedulerPath`""
    )

    Set-Content -LiteralPath $LauncherPath -Value $Lines -Encoding ASCII

    Write-Host "Startup launcher installed:" -ForegroundColor Green
    Write-Host "  $LauncherPath"
    Write-Host "It will start automatically when this user logs in."
    Write-Host "Start now:"
    Write-Host "  & `"$LauncherPath`""
}

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description $Description `
        -Force | Out-Null

    Write-Host "Scheduled task installed: $TaskName" -ForegroundColor Green
    Write-Host "User: $CurrentUser"
    Write-Host "It will start automatically when this user logs in."
    Write-Host "Start now:"
    Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
    Write-Host "Check status:"
    Write-Host "  Get-ScheduledTask -TaskName `"$TaskName`" | Get-ScheduledTaskInfo"
}
catch {
    Write-Warning "Scheduled task registration failed: $($_.Exception.Message)"
    Write-Warning "Falling back to the current user's Startup folder."
    Install-StartupLauncher
}
