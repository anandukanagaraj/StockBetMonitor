param(
    [string]$Message = "Update portfolio report",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReportsDir = Join-Path $ProjectRoot "reports"
$ConfigPath = Join-Path $ProjectRoot "config.ini"
$PublicDir = Join-Path $ProjectRoot "public"

if ($env:MPB_PUBLIC_DIR) {
    $PublicDir = $env:MPB_PUBLIC_DIR
}
elseif (Test-Path $ConfigPath) {
    $inPublishSection = $false
    foreach ($line in Get-Content -LiteralPath $ConfigPath) {
        $trimmed = $line.Trim()
        if ($trimmed -match '^\[(.+)\]$') {
            $inPublishSection = ($matches[1] -eq "PUBLISH")
            continue
        }
        if ($inPublishSection -and $trimmed -match '^PUBLIC_DIR\s*=\s*(.+)$') {
            $configuredPublicDir = $matches[1].Trim()
            if ($configuredPublicDir) {
                $PublicDir = $configuredPublicDir
            }
            break
        }
    }
}

if (-not (Test-Path $PublicDir)) {
    $SiblingV2PublicDir = Join-Path $ProjectRoot "public"
    if (Test-Path $SiblingV2PublicDir) {
        Write-Warning "Configured public folder not found: $PublicDir"
        Write-Warning "Using existing GitHub Pages folder: $SiblingV2PublicDir"
        $PublicDir = $SiblingV2PublicDir
    }
}

$IndexPath = Join-Path $PublicDir "index.html"

if (-not (Test-Path $PublicDir)) {
    throw "Public repository folder not found: $PublicDir"
}

$gitCommand = Get-Command git -ErrorAction SilentlyContinue
$gitPath = if ($gitCommand) { $gitCommand.Source } else { $null }

if (-not $gitPath) {
    $commonGitPaths = @(
        "$env:ProgramFiles\Git\cmd\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
    )

    $gitPath = $commonGitPaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

if (-not $gitPath) {
    throw "Git was not found. Install Git for Windows or add git.exe to PATH, then run this script again."
}

$latest = Get-ChildItem -Path $ReportsDir -Filter "report_masked_*.html" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $latest) {
    throw "No masked report found in $ReportsDir. Generate a report first."
}

Copy-Item -LiteralPath $latest.FullName -Destination $IndexPath -Force
Write-Host "Copied latest masked report:" -ForegroundColor Green
Write-Host "  $($latest.FullName)"
Write-Host "to:"
Write-Host "  $IndexPath"

Push-Location $PublicDir
try {
    $safeDirectoryArg = "safe.directory=$PublicDir"
    $status = & $gitPath -c $safeDirectoryArg status --short index.html
    if ($LASTEXITCODE -ne 0) {
        throw "Git status failed in $PublicDir"
    }

    if (-not $status) {
        Write-Host "No index.html changes to publish." -ForegroundColor Yellow
        exit 0
    }

    & $gitPath -c $safeDirectoryArg add index.html
    if ($LASTEXITCODE -ne 0) {
        throw "Git add failed in $PublicDir"
    }

    & $gitPath -c $safeDirectoryArg commit -m $Message
    if ($LASTEXITCODE -ne 0) {
        throw "Git commit failed in $PublicDir"
    }

    if ($NoPush) {
        Write-Host "Committed but skipped push because -NoPush was provided." -ForegroundColor Yellow
    } else {
        & $gitPath -c $safeDirectoryArg push -u origin main
        if ($LASTEXITCODE -ne 0) {
            throw "Git push failed in $PublicDir"
        }
        Write-Host "Published report to GitHub Pages repository." -ForegroundColor Green
    }
}
finally {
    Pop-Location
}
