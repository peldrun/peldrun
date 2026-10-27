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

if (-not $env:PELDRUN_BACKEND_URL) {
    $defaultBackendPort = if ($env:PELDRUN_BACKEND_PORT) { [int]$env:PELDRUN_BACKEND_PORT } else { 8088 }
    $env:PELDRUN_BACKEND_URL = "http://127.0.0.1:${defaultBackendPort}"
}

# ---------------------------------------------------------------------
# Helpers: animated spinner + aligned status line
#
# We use System.Diagnostics.Process directly (instead of & / Start-Process)
# so we get a live handle on the child process. That lets us animate a
# spinner while the child runs, then read its stdout/stderr/ExitCode
# reliably. A plain & call blocks the console and shows no feedback.
# ---------------------------------------------------------------------
function Invoke-WithSpinner {
    param(
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string]$Arguments,
        [int]$TimeoutSeconds = 60,
        [ConsoleColor]$Color = [ConsoleColor]::Cyan
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $FilePath
    $psi.Arguments              = $Arguments
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true

    try {
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8
    } catch { }

    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        Write-Host "`r$(' ' * ($Message.Length + 12))`r" -NoNewline
        return [PSCustomObject]@{ ExitCode = -1; StdOut = ""; StdErr = $_.Exception.Message }
    }

    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()

    $spinner  = '|', '/', '-', '\'
    $idx      = 0
    $clearLen = $Message.Length + 12
    $sw       = [System.Diagnostics.Stopwatch]::StartNew()

    while (-not $proc.HasExited) {
        if ($sw.Elapsed.TotalSeconds -gt $TimeoutSeconds) {
            try { $proc.Kill() } catch { }
            Write-Host "`r$(' ' * $clearLen)`r" -NoNewline
            return [PSCustomObject]@{ ExitCode = -1; StdOut = ""; StdErr = "Timed out after ${TimeoutSeconds}s" }
        }
        Write-Host "`r    $($spinner[$idx++ % 4]) $Message " -NoNewline -ForegroundColor $Color
        Start-Sleep -Milliseconds 100
    }

    $proc.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result

    Write-Host "`r$(' ' * $clearLen)`r" -NoNewline

    return [PSCustomObject]@{ ExitCode = $proc.ExitCode; StdOut = $stdout; StdErr = $stderr }
}

function Write-Status {
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('OK','FAIL','WARN')][string]$Status = 'OK'
    )
    $label = switch ($Status) { 'OK' { '[OK]  ' } 'FAIL' { '[FAIL]' } 'WARN' { '[WARN]' } }
    $color = switch ($Status) { 'OK' { 'Green' } 'FAIL' { 'Red' } 'WARN' { 'Yellow' } }
    Write-Host "    $label $Message" -ForegroundColor $color
}

# ---------------------------------------------------------------------
# Preflight header
# ---------------------------------------------------------------------
Write-Host ""
Write-Host ">>> Running preflight checks..." -ForegroundColor Cyan

# ---------------------------------------------------------------------
# Preflight 1a: locate node.exe
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
    Write-Status "Locate Node.js" FAIL
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
Write-Status "Node.js located: $NodeExe" OK

# ---------------------------------------------------------------------
# Preflight 1b: verify Node.js version (spinner - launching node.exe
# is the only slow step in the whole preflight sequence)
# ---------------------------------------------------------------------
$nodeVer = Invoke-WithSpinner `
    -Message         "Checking Node.js version..." `
    -FilePath        $NodeExe `
    -Arguments       '--version' `
    -TimeoutSeconds  30

$nodeVersionRaw = $nodeVer.StdOut.Trim()

if ($nodeVer.ExitCode -ne 0) {
    Write-Status "Node.js version check" FAIL
    Write-Host "[ERROR] Could not run '$NodeExe --version'." -ForegroundColor Red
    if ($nodeVer.StdErr) { Write-Host $nodeVer.StdErr -ForegroundColor DarkRed }
    exit 1
}

$nodeVersionOk = $false
if ($nodeVersionRaw -match "v(\d+)\.(\d+)\.(\d+)") {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $nodeVersionOk = ($major -gt 20 -or ($major -eq 20 -and $minor -ge 9))
}

if (-not $nodeVersionOk) {
    Write-Status "Node.js version $nodeVersionRaw" FAIL
    Write-Host "[ERROR] Node.js $nodeVersionRaw is too old (Next.js 16 requires >= 20.9.0)." -ForegroundColor Red
    Write-Host "        Re-run .\setup.ps1 to upgrade, or install manually:" -ForegroundColor Yellow
    Write-Host "        https://nodejs.org/en/download/" -ForegroundColor White
    exit 1
}
Write-Status "Node.js $nodeVersionRaw (>= 20.9.0)" OK

# ---------------------------------------------------------------------
# Preflight 2: verify frontend dependencies are installed
# ---------------------------------------------------------------------
$NodeModules = Join-Path $FrontendDir "node_modules"
$NextModule  = Join-Path $NodeModules "next"

if (-not (Test-Path $NodeModules)) {
    Write-Status "frontend\node_modules" FAIL
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
Write-Status "frontend\node_modules present" OK

if (-not (Test-Path $NextModule)) {
    Write-Status "Next.js in node_modules" FAIL
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
Write-Status "Next.js installed in node_modules" OK

# ---------------------------------------------------------------------
# Preflight 3: verify npm is available
# ---------------------------------------------------------------------
$NodeDir = Split-Path -Parent $NodeExe
$NpmCmd  = Join-Path $NodeDir "npm.cmd"
if (-not (Test-Path $NpmCmd)) {
    $resolvedNpm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if ($resolvedNpm) { $NpmCmd = $resolvedNpm }
    else {
        Write-Status "npm.cmd" FAIL
        Write-Host "[ERROR] npm.cmd not found alongside node.exe or in PATH." -ForegroundColor Red
        Write-Host "        Reinstall Node.js: https://nodejs.org/en/download/" -ForegroundColor Yellow
        exit 1
    }
}
Write-Status "npm.cmd located" OK

Write-Host ""

# ---------------------------------------------------------------------
# Startup banner (cyan - frontend)
# ---------------------------------------------------------------------
Write-Host "███████████  ██████████ █████       ██████████   ███████████   █████  █████ ██████   █████" -ForegroundColor Cyan
Write-Host "░░███░░░░░███░░███░░░░░█░░███       ░░███░░░░███ ░░███░░░░░███ ░░███  ░░███ ░░██████ ░░███ " -ForegroundColor Cyan
Write-Host " ░███    ░███ ░███  █ ░  ░███        ░███   ░░███ ░███    ░███  ░███   ░███  ░███░███ ░███ " -ForegroundColor Cyan
Write-Host " ░██████████  ░██████    ░███        ░███    ░███ ░██████████   ░███   ░███  ░███░░███░███ " -ForegroundColor Cyan
Write-Host " ░███░░░░░░   ░███░░█    ░███        ░███    ░███ ░███░░░░░███  ░███   ░███  ░███ ░░██████ " -ForegroundColor Cyan
Write-Host " ░███         ░███ ░   █ ░███      █ ░███    ███  ░███    ░███  ░███   ░███  ░███  ░░█████ " -ForegroundColor Cyan
Write-Host " █████        ██████████ ███████████ ██████████   █████   █████ ░░████████   █████  ░░█████" -ForegroundColor Cyan
Write-Host "░░░░░        ░░░░░░░░░░ ░░░░░░░░░░░ ░░░░░░░░░░   ░░░░░   ░░░░░   ░░░░░░░░   ░░░░░    ░░░░░" -ForegroundColor Cyan
Write-Host ""

Write-Host ">>> Starting peldrun Web Frontend in FAST DEV MODE (Hot-Reload)..." -ForegroundColor Cyan
Write-Host "    Project root: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Frontend dir: $FrontendDir" -ForegroundColor DarkGray
Write-Host "    Node.js:      $nodeVersionRaw ($NodeExe)" -ForegroundColor DarkGray
Write-Host "    Endpoint:     http://localhost:${FrontendPort}" -ForegroundColor DarkGray
Write-Host "    Backend URL:  $env:PELDRUN_BACKEND_URL (for API rewrites)" -ForegroundColor DarkGray

# ---------------------------------------------------------------------
# Free the target port if occupied
# ---------------------------------------------------------------------
Write-Host ""
Write-Host ">>> Freeing port ${FrontendPort} if occupied..." -ForegroundColor Cyan

Get-NetTCPConnection -LocalPort $FrontendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

Write-Status "Port ${FrontendPort} is free" OK
Write-Host ""

# ---------------------------------------------------------------------
# Launch Next.js dev server
#
# We invoke the local next.cmd directly to avoid the npm exec issue with
# the -p flag (npm exec treats -p as its own --package flag and does not
# pass it to next).
# ---------------------------------------------------------------------
$NextCmd = Join-Path $NodeModules ".bin\next.cmd"
if (-not (Test-Path $NextCmd)) {
    Write-Host "[ERROR] next.cmd not found in node_modules\.bin" -ForegroundColor Red
    Write-Host "        Fix: cd $FrontendDir; npm ci" -ForegroundColor Yellow
    exit 1
}

Write-Host ">>> Launching Next.js Fast Dev..." -ForegroundColor Green
Write-Host "    Frontend is live at http://localhost:${FrontendPort}" -ForegroundColor Green
Write-Host "    Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

& $NextCmd dev -p $FrontendPort
