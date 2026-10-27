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

# ---------------------------------------------------------------------
# Configuration (env overrides, with safe defaults)
# ---------------------------------------------------------------------
$FrontendPort = if ($env:PELDRUN_FRONTEND_PORT) { [int]$env:PELDRUN_FRONTEND_PORT } else { 3088 }

if ($FrontendPort -lt 1 -or $FrontendPort -gt 65535) {
    Write-Host "[ERROR] Invalid port: $FrontendPort (must be 1-65535)" -ForegroundColor Red
    exit 1
}

# Default the backend URL for Next.js rewrites if not already set.
if (-not $env:PELDRUN_BACKEND_URL) {
    $defaultBackendPort = if ($env:PELDRUN_BACKEND_PORT) { [int]$env:PELDRUN_BACKEND_PORT } else { 8088 }
    $env:PELDRUN_BACKEND_URL = "http://127.0.0.1:${defaultBackendPort}"
}

# ---------------------------------------------------------------------
# Preflight 1: verify Node.js is installed and >= 20.9.0
#
# Next.js 16 requires Node.js 20.9.0 or newer. Launching with an older
# version produces a confusing error from inside the bundler instead of
# a clear instruction to run setup.ps1.
# ---------------------------------------------------------------------
$NodeExe = $null
foreach ($cmd in @("node", "nodejs")) {
    try {
        $resolved = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
        if ($resolved -and (Test-Path $resolved)) {
            $NodeExe = $resolved
            break
        }
    } catch { }
}

if (-not $NodeExe) {
    foreach ($candidate in @(
        "$env:ProgramFiles\nodejs\node.exe",
        "$env:LOCALAPPDATA\Programs\nodejs\node.exe"
    )) {
        if (Test-Path $candidate) { $NodeExe = $candidate; break }
    }
}

if (-not $NodeExe) {
    Write-Host "[ERROR] Node.js is not installed." -ForegroundColor Red
    Write-Host ""
    Write-Host "First-time setup required. Run:" -ForegroundColor Yellow
    Write-Host "  cd $ProjectRoot" -ForegroundColor White
    Write-Host "  .\setup.ps1" -ForegroundColor White
    Write-Host ""
    Write-Host "Or install Node.js manually (>= 20.9.0):" -ForegroundColor Yellow
    Write-Host "  https://nodejs.org/en/download/" -ForegroundColor White
    exit 1
}

$nodeVersionRaw = & $NodeExe --version 2>&1
$nodeVersionOk = $false
if ($nodeVersionRaw -match "v(\d+)\.(\d+)\.(\d+)") {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $nodeVersionOk = ($major -gt 20 -or ($major -eq 20 -and $minor -ge 9))
}

if (-not $nodeVersionOk) {
    Write-Host "[ERROR] Node.js $nodeVersionRaw is too old (Next.js 16 requires >= 20.9.0)." -ForegroundColor Red
    Write-Host "        Re-run .\setup.ps1 to upgrade, or install manually:" -ForegroundColor Yellow
    Write-Host "        https://nodejs.org/en/download/" -ForegroundColor White
    exit 1
}

# ---------------------------------------------------------------------
# Preflight 2: verify frontend dependencies are installed
#
# node_modules/ must contain Next.js. If not, setup.ps1 has not been run
# (or the installation was incomplete). Running npx next without a local
# Next.js install either downloads it globally or fails obscurely.
# ---------------------------------------------------------------------
$NodeModules = Join-Path $FrontendDir "node_modules"
$NextModule  = Join-Path $NodeModules "next"

if (-not (Test-Path $NodeModules)) {
    Write-Host "[ERROR] frontend\node_modules is missing." -ForegroundColor Red
    Write-Host ""
    Write-Host "Frontend dependencies have not been installed yet." -ForegroundColor Yellow
    Write-Host "Run first-time setup:" -ForegroundColor Yellow
    Write-Host "  cd $ProjectRoot" -ForegroundColor White
    Write-Host "  .\setup.ps1" -ForegroundColor White
    Write-Host ""
    Write-Host "Or install manually:" -ForegroundColor Yellow
    Write-Host "  cd $FrontendDir" -ForegroundColor White
    Write-Host "  npm ci" -ForegroundColor White
    exit 1
}

if (-not (Test-Path $NextModule)) {
    Write-Host "[ERROR] Next.js is not installed in frontend\node_modules." -ForegroundColor Red
    Write-Host ""
    Write-Host "The frontend dependency tree is incomplete." -ForegroundColor Yellow
    Write-Host "Fix it with:" -ForegroundColor Yellow
    Write-Host "  cd $FrontendDir" -ForegroundColor White
    Write-Host "  npm ci" -ForegroundColor White
    Write-Host ""
    Write-Host "If npm ci fails, try:" -ForegroundColor Yellow
    Write-Host "  npm install" -ForegroundColor White
    exit 1
}

# ---------------------------------------------------------------------
# Preflight 3: verify npm is available
# ---------------------------------------------------------------------
$NodeDir = Split-Path -Parent $NodeExe
$NpmCmd = Join-Path $NodeDir "npm.cmd"
if (-not (Test-Path $NpmCmd)) {
    $resolvedNpm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if ($resolvedNpm) { $NpmCmd = $resolvedNpm }
    else {
        Write-Host "[ERROR] npm.cmd not found alongside node.exe or in PATH." -ForegroundColor Red
        Write-Host "        Reinstall Node.js: https://nodejs.org/en/download/" -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------
Write-Host ">>> Starting peldrun Web Frontend in FAST DEV MODE (Hot-Reload)..." -ForegroundColor Cyan
Write-Host "    Project root: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Frontend dir: $FrontendDir" -ForegroundColor DarkGray
Write-Host "    Node.js:      $($nodeVersionRaw.Trim()) ($NodeExe)" -ForegroundColor DarkGray
Write-Host "    Endpoint:     http://localhost:${FrontendPort}" -ForegroundColor DarkGray
Write-Host "    Backend URL:  $env:PELDRUN_BACKEND_URL (for API rewrites)" -ForegroundColor DarkGray

# ---------------------------------------------------------------------
# Free the target port if occupied
# ---------------------------------------------------------------------
Get-NetTCPConnection -LocalPort $FrontendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------------
# Launch Next.js dev server
#
# Uses the local Next.js from node_modules (via `npx`), not a global one.
# Since we verified node_modules/next exists above, npx resolves to the
# local install and starts in dev mode with hot-reload.
# ---------------------------------------------------------------------
Write-Host ">>> Launching Next.js Fast Dev on http://localhost:${FrontendPort}..." -ForegroundColor Green

& $NpmCmd exec next dev -p $FrontendPort
