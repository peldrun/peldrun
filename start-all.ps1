# start-all.ps1
#
# Launches both the backend and the frontend in independent windows.
#
# Works from any location: the script derives the project root from
# its own path ($PSScriptRoot), so moving the repository to a
# different drive or folder does not break it.
#
# Configurable via environment variables (all optional):
#   PELDRUN_BACKEND_PORT   - backend port  (default: 8088)
#   PELDRUN_FRONTEND_PORT  - frontend port (default: 3088)
#
# Any PELDRUN_* variables present in the current environment are
# forwarded to the child windows, so calling `start-all.ps1` after
# setting (for example) `$env:PELDRUN_BACKEND_PORT = "8090"` makes
# both servers start on the same non-default port.

$ErrorActionPreference = "Stop"

Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "   Launching peldrun Web Dashboard (All)       " -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan

# ---- Resolve project root from this script's own location --------------
$ProjectRoot = if ($PSScriptRoot) {
    $PSScriptRoot
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}

$BackendScript  = Join-Path $ProjectRoot "start-backend.ps1"
$FrontendScript = Join-Path $ProjectRoot "start-frontend.ps1"

# ---- Sanity check: both sibling scripts must exist ---------------------
$missing = @()
if (-not (Test-Path $BackendScript))  { $missing += $BackendScript }
if (-not (Test-Path $FrontendScript)) { $missing += $FrontendScript }

if ($missing.Count -gt 0) {
    Write-Host "[ERROR] Required launcher script(s) not found:" -ForegroundColor Red
    foreach ($m in $missing) { Write-Host "        - $m" -ForegroundColor Red }
    Write-Host "        Both start-backend.ps1 and start-frontend.ps1 must" -ForegroundColor Yellow
    Write-Host "        live in the same directory as this script." -ForegroundColor Yellow
    exit 1
}

# ---- Port display (read from env, same defaults as the child scripts) --
$BackendPort  = if ($env:PELDRUN_BACKEND_PORT)  { [int]$env:PELDRUN_BACKEND_PORT }  else { 8088 }
$FrontendPort = if ($env:PELDRUN_FRONTEND_PORT) { [int]$env:PELDRUN_FRONTEND_PORT } else { 3088 }

Write-Host "    Project root:   $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Backend script: $BackendScript" -ForegroundColor DarkGray
Write-Host "    Frontend:       $FrontendScript" -ForegroundColor DarkGray
Write-Host "    Backend port:   $BackendPort" -ForegroundColor DarkGray
Write-Host "    Frontend port:  $FrontendPort" -ForegroundColor DarkGray

# ---- Start Backend in an independent window ----------------------------
Start-Process powershell -ArgumentList "-NoExit", "-File", "`"$BackendScript`""

# Wait 2 seconds for backend initialization
Start-Sleep -Seconds 2

# ---- Start Frontend in an independent window ---------------------------
Start-Process powershell -ArgumentList "-NoExit", "-File", "`"$FrontendScript`""

Write-Host "`n>>> Both servers launched in background windows." -ForegroundColor Green
Write-Host ">>> Access Dashboard UI at: http://localhost:$FrontendPort" -ForegroundColor Yellow
Write-Host ">>> Backend API at:         http://localhost:$BackendPort" -ForegroundColor Yellow
