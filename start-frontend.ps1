# start-frontend.ps1
#
# Frontend launcher for PELDRUN Web.
#
# Works from any location: the script derives the project root from
# its own path ($PSScriptRoot).
#
# Configurable via environment variables (all optional):
#   PELDRUN_FRONTEND_PORT  - port to bind (default: 3088)
#   PELDRUN_BACKEND_URL    - backend origin for API rewrites
#                            (default: http://127.0.0.1:8088)

$ErrorActionPreference = "Stop"

# Resolve the project root from the script's own location.
$ProjectRoot = if ($PSScriptRoot) {
    $PSScriptRoot
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$FrontendDir = Join-Path $ProjectRoot "frontend"

if (-not (Test-Path $FrontendDir)) {
    Write-Host "[ERROR] Frontend directory not found: $FrontendDir" -ForegroundColor Red
    Write-Host "        Expected to find 'frontend' next to this script." -ForegroundColor Yellow
    exit 1
}

Set-Location -Path $FrontendDir

# ---- Configuration (env overrides, with safe defaults) -------------------
$FrontendPort = if ($env:PELDRUN_FRONTEND_PORT) { [int]$env:PELDRUN_FRONTEND_PORT } else { 3088 }

if ($FrontendPort -lt 1 -or $FrontendPort -gt 65535) {
    Write-Host "[ERROR] Invalid port: $FrontendPort (must be 1-65535)" -ForegroundColor Red
    exit 1
}

# Default the backend URL for Next.js rewrites if not already set.
# This keeps the frontend pointed at the backend even if the operator
# forgets to export PELDRUN_BACKEND_URL before running the script.
if (-not $env:PELDRUN_BACKEND_URL) {
    $defaultBackendPort = if ($env:PELDRUN_BACKEND_PORT) { [int]$env:PELDRUN_BACKEND_PORT } else { 8088 }
    $env:PELDRUN_BACKEND_URL = "http://127.0.0.1:${defaultBackendPort}"
}

# ---- Startup banner ------------------------------------------------------
Write-Host ">>> Starting peldrun Web Frontend in FAST DEV MODE (Hot-Reload)..." -ForegroundColor Cyan
Write-Host "    Project root: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Frontend dir: $FrontendDir" -ForegroundColor DarkGray
Write-Host "    Endpoint:     http://localhost:${FrontendPort}" -ForegroundColor DarkGray
Write-Host "    Backend URL:  $env:PELDRUN_BACKEND_URL (for API rewrites)" -ForegroundColor DarkGray

# ---- Free the target port if occupied -----------------------------------
Get-NetTCPConnection -LocalPort $FrontendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

# ---- Launch Next.js dev server -----------------------------------------
Write-Host ">>> Launching Next.js Fast Dev on http://localhost:${FrontendPort}..." -ForegroundColor Green
npx next dev -p $FrontendPort
