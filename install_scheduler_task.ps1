param(
    [string]$TaskName = "StockMonitorV3Scheduler",
    [string]$WebTaskName = "StockMonitorV3WebApp",
    [switch]$SkipWebApp
)

# Registers Windows logon tasks so both the scheduler and the web app UI
# (http://127.0.0.1:5002/) start automatically after a restart/login.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
# pythonw.exe runs the web app without a console window; it logs to logs\.
$PythonwPath = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$SchedulerPath = Join-Path $ProjectRoot "scheduler.py"
$AppPath = Join-Path $ProjectRoot "app.py"

foreach ($required in @($PythonPath, $PythonwPath, $SchedulerPath, $AppPath)) {
    if (-not (Test-Path $required)) {
        throw "Required file not found: $required"
    }
}

$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"

function Install-StartupLauncher {
    param([string]$Name, [string]$Python, [string]$Script, [string]$Comment)

    $StartupDir = [Environment]::GetFolderPath("Startup")
    $LauncherPath = Join-Path $StartupDir "$Name.cmd"
    $Lines = @(
        "@echo off",
        "REM $Comment",
        "cd /d `"$ProjectRoot`"",
        "start `"`" `"$Python`" `"$Script`""
    )
    Set-Content -LiteralPath $LauncherPath -Value $Lines -Encoding ASCII
    Write-Host "Startup launcher installed: $LauncherPath" -ForegroundColor Green
}

function Install-LogonTask {
    param([string]$Name, [string]$Python, [string]$Script, [string]$Description)

    $Action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument "`"$Script`"" `
        -WorkingDirectory $ProjectRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $CurrentUser `
        -LogonType Interactive `
        -RunLevel Limited

    try {
        Register-ScheduledTask `
            -TaskName $Name `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -Principal $Principal `
            -Description $Description `
            -Force | Out-Null
        Write-Host "Scheduled task installed: $Name (starts at logon for $CurrentUser)" -ForegroundColor Green
    }
    catch {
        if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
            # Typically "Access is denied" when the task was created from an elevated
            # prompt. The existing task still starts at logon, so keep it rather than
            # adding a Startup launcher that would start a second copy.
            Write-Warning "Could not update existing task ${Name}: $($_.Exception.Message)"
            Write-Warning "Keeping the existing task. Rerun from an elevated PowerShell to update it."
            return
        }
        Write-Warning "Scheduled task registration failed for ${Name}: $($_.Exception.Message)"
        Write-Warning "Falling back to the current user's Startup folder."
        Install-StartupLauncher -Name $Name -Python $Python -Script $Script -Comment $Description
    }
}

Install-LogonTask `
    -Name $TaskName `
    -Python $PythonPath `
    -Script $SchedulerPath `
    -Description "Runs StockMonitor V3 scheduler for market checks, alerts, EOD email, and report publishing."

if (-not $SkipWebApp) {
    Install-LogonTask `
        -Name $WebTaskName `
        -Python $PythonwPath `
        -Script $AppPath `
        -Description "Runs the StockMonitor V3 web app UI at http://127.0.0.1:5002/."
}

Write-Host ""
Write-Host "Start now:"
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
if (-not $SkipWebApp) {
    Write-Host "  Start-ScheduledTask -TaskName `"$WebTaskName`""
}
Write-Host "Check status:"
Write-Host "  Get-ScheduledTask -TaskName `"StockMonitorV3*`" | Get-ScheduledTaskInfo"
