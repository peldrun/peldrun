# start-backend.ps1
#
# Backend launcher for PELDRUN Web.
#
# Works from any location: the script derives the project root from
# its own path ($PSScriptRoot), so moving the repository to a
# different drive or folder does not break it.
#
# Configurable via environment variables (all optional):
#   PELDRUN_BACKEND_HOST  - host to bind         (default: 0.0.0.0)
#   PELDRUN_BACKEND_PORT  - port to bind         (default: 8088)
#   PELDRUN_VENV_PYTHON   - explicit venv python (default: auto-detect)

$ErrorActionPreference = "Stop"

# Resolve the project root from the script's own location.
# $PSScriptRoot is reliable in PowerShell 3.0+. Fall back to
# $MyInvocation.MyCommand.Path for older versions or unusual hosts.
$ProjectRoot = if ($PSScriptRoot) {
    $PSScriptRoot
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$BackendDir = Join-Path $ProjectRoot "backend"

if (-not (Test-Path $BackendDir)) {
    Write-Host "[ERROR] Backend directory not found: $BackendDir" -ForegroundColor Red
    Write-Host "        Expected to find 'backend' next to this script." -ForegroundColor Yellow
    exit 1
}

Set-Location -Path $BackendDir

# ---- Configuration (env overrides, with safe defaults) -------------------
$BackendHost = if ($env:PELDRUN_BACKEND_HOST) { $env:PELDRUN_BACKEND_HOST } else { "0.0.0.0" }
$BackendPort = if ($env:PELDRUN_BACKEND_PORT) { [int]$env:PELDRUN_BACKEND_PORT } else { 8088 }

if ($BackendPort -lt 1 -or $BackendPort -gt 65535) {
    Write-Host "[ERROR] Invalid port: $BackendPort (must be 1-65535)" -ForegroundColor Red
    exit 1
}

# ---- Python executable detection ----------------------------------------
# Priority: env override > venv (Windows) > venv (POSIX) > error.
if ($env:PELDRUN_VENV_PYTHON -and (Test-Path $env:PELDRUN_VENV_PYTHON)) {
    $PythonExe = $env:PELDRUN_VENV_PYTHON
} else {
    $venvWin   = Join-Path $BackendDir ".venv\Scripts\python.exe"
    $venvPosix = Join-Path $BackendDir ".venv/bin/python"
    if (Test-Path $venvWin) {
        $PythonExe = $venvWin
    } elseif (Test-Path $venvPosix) {
        $PythonExe = $venvPosix
    } else {
        Write-Host "[ERROR] Virtual environment not found in $BackendDir\.venv" -ForegroundColor Red
        Write-Host "        Expected: .venv\Scripts\python.exe (Windows) or .venv/bin/python (POSIX)" -ForegroundColor Yellow
        Write-Host "        Override with: `$env:PELDRUN_VENV_PYTHON = '<full-path-to-python.exe>'" -ForegroundColor Yellow
        exit 1
    }
}

# ---- Startup banner ------------------------------------------------------
Write-Host ">>> Starting peldrun Web Backend (FastAPI 2.0)..." -ForegroundColor Cyan
Write-Host "    Project root: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Backend dir:  $BackendDir" -ForegroundColor DarkGray
Write-Host "    Python:       $PythonExe" -ForegroundColor DarkGray
Write-Host "    Endpoint:     http://${BackendHost}:${BackendPort}" -ForegroundColor DarkGray

# ---- Free the target port if occupied -----------------------------------
Get-NetTCPConnection -LocalPort $BackendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

# ---- Launch Uvicorn -----------------------------------------------------
Write-Host ">>> Launching Uvicorn with Windows Proactor loop on http://${BackendHost}:${BackendPort}..." -ForegroundColor Green

& $PythonExe -c "import sys, asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy()) if sys.platform == 'win32' else None; uvicorn.run('omweb.main:app', host='$BackendHost', port=$BackendPort, reload=False)"
