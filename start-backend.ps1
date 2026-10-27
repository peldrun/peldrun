# start-backend.ps1
#
# Backend launcher for PELDRUN Web.
#
# ALWAYS forces the project-local browser directory
# (\.playwright-browsers\) regardless of any inherited
# PLAYWRIGHT_BROWSERS_PATH. This guarantees the backend never uses
# browsers belonging to another project on the same machine.

$ErrorActionPreference = "Stop"

$ProjectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$BackendDir = Join-Path $ProjectRoot "backend"

if (-not (Test-Path $BackendDir)) {
    Write-Host "[ERROR] Backend directory not found: $BackendDir" -ForegroundColor Red
    exit 1
}

Set-Location -Path $BackendDir

# ---------------------------------------------------------------------
# Preflight: ensure setup.ps1 has been run
# ---------------------------------------------------------------------
$VenvWin = Join-Path $BackendDir ".venv\Scripts\python.exe"
$VenvPosix = Join-Path $BackendDir ".venv/bin/python"

if ($env:PELDRUN_VENV_PYTHON -and (Test-Path $env:PELDRUN_VENV_PYTHON)) {
    $PythonExe = $env:PELDRUN_VENV_PYTHON
} elseif (Test-Path $VenvWin) {
    $PythonExe = $VenvWin
} elseif (Test-Path $VenvPosix) {
    $PythonExe = $VenvPosix
} else {
    Write-Host "[ERROR] Virtual environment not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "First-time setup required. Run:" -ForegroundColor Yellow
    Write-Host "  cd $ProjectRoot" -ForegroundColor White
    Write-Host "  .\setup.ps1" -ForegroundColor White
    exit 1
}

# ---------------------------------------------------------------------
# Preflight: verify uvicorn is importable
# ---------------------------------------------------------------------
$uvicornCheck = & $PythonExe -c "import uvicorn; print('OK')" 2>$null
if ($uvicornCheck -ne "OK") {
    Write-Host "[ERROR] uvicorn is not installed in .venv." -ForegroundColor Red
    Write-Host "        Run: .\setup.ps1" -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------------
# Preflight: verify the ASGI app imports
# ---------------------------------------------------------------------
$appCheck = & $PythonExe -c "import omweb.main; print('OK')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to import omweb.main (ASGI app)." -ForegroundColor Red
    Write-Host $appCheck -ForegroundColor DarkRed
    exit 1
}

# ---------------------------------------------------------------------
# Force project-local Playwright browser directory.
# This ALWAYS overrides any inherited PLAYWRIGHT_BROWSERS_PATH so the
# running backend can only use browsers installed by setup.ps1.
# ---------------------------------------------------------------------
$BrowserDir = Join-Path $ProjectRoot ".playwright-browsers"
if (-not (Test-Path $BrowserDir)) {
    New-Item -ItemType Directory -Path $BrowserDir | Out-Null
}
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserDir

# ---------------------------------------------------------------------
# Preflight: warn (do not block) if Playwright browsers are missing
# ---------------------------------------------------------------------
if ($env:PELDRUN_SKIP_BROWSER_CHECK -ne "1") {
    $pwCheck = @"
import os
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        print('OK' if os.path.exists(p.chromium.executable_path) else 'MISSING')
except Exception:
    print('MISSING')
"@
    $pwResult = & $PythonExe -c $pwCheck 2>$null | Select-Object -Last 1
    if ($pwResult -ne "OK") {
        Write-Host "[WARN] Playwright Chromium not found at:" -ForegroundColor Yellow
        Write-Host "       $BrowserDir" -ForegroundColor Yellow
        Write-Host "       browser_interactive will be unavailable." -ForegroundColor Yellow
        Write-Host "       Fix: .\setup.ps1" -ForegroundColor Yellow
        Write-Host ""
    }
}

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
$BackendHost = if ($env:PELDRUN_BACKEND_HOST) { $env:PELDRUN_BACKEND_HOST } else { "0.0.0.0" }
$BackendPort = if ($env:PELDRUN_BACKEND_PORT) { [int]$env:PELDRUN_BACKEND_PORT } else { 8088 }

if ($BackendPort -lt 1 -or $BackendPort -gt 65535) {
    Write-Host "[ERROR] Invalid port: $BackendPort (must be 1-65535)" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------
Write-Host ">>> Starting peldrun Web Backend (FastAPI 2.0)..." -ForegroundColor Cyan
Write-Host "    Project root: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Backend dir:  $BackendDir" -ForegroundColor DarkGray
Write-Host "    Python:       $PythonExe" -ForegroundColor DarkGray
Write-Host "    Browsers:     $BrowserDir" -ForegroundColor DarkGray
Write-Host "    Endpoint:     http://${BackendHost}:${BackendPort}" -ForegroundColor DarkGray

# ---------------------------------------------------------------------
# Free the target port if occupied
# ---------------------------------------------------------------------
Get-NetTCPConnection -LocalPort $BackendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------------
# Launch Uvicorn
# ---------------------------------------------------------------------
Write-Host ">>> Launching Uvicorn..." -ForegroundColor Green

& $PythonExe -c "import sys, asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy()) if sys.platform == 'win32' else None; uvicorn.run('omweb.main:app', host='$BackendHost', port=$BackendPort, reload=False)"
