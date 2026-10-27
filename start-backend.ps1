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
$BackendDir  = Join-Path $ProjectRoot "backend"

if (-not (Test-Path $BackendDir)) {
    Write-Host "[ERROR] Backend directory not found: $BackendDir" -ForegroundColor Red
    exit 1
}

Set-Location -Path $BackendDir

# ---------------------------------------------------------------------
# Helpers: animated spinner + aligned status line
#
# System.Diagnostics.Process is used directly (instead of & / Start-Process)
# so we get a live handle on the child process, animate a spinner while
# it runs, and read stdout/stderr/ExitCode reliably.
# ---------------------------------------------------------------------
function Invoke-WithSpinner {
    param(
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string]$Arguments,
        [string]$StandardInput = $null,
        [int]$TimeoutSeconds = 180,
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

    if ($null -ne $StandardInput) {
        $psi.RedirectStandardInput = $true
        try { $psi.StandardInputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    }

    # Explicitly propagate the parent PYTHONPATH so the child Python
    # interpreter can resolve the omweb package even after we modified
    # it above.
    if ($env:PYTHONPATH) {
        $psi.EnvironmentVariables["PYTHONPATH"] = $env:PYTHONPATH
    }

    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        Write-Host "`r$(' ' * ($Message.Length + 12))`r" -NoNewline
        return [PSCustomObject]@{ ExitCode = -1; StdOut = ""; StdErr = $_.Exception.Message }
    }

    if ($null -ne $StandardInput) {
        $proc.StandardInput.Write($StandardInput)
        $proc.StandardInput.Close()
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
# Preflight: ensure setup.ps1 has been run
# ---------------------------------------------------------------------
$VenvWin   = Join-Path $BackendDir ".venv\Scripts\python.exe"
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
Write-Status "Virtual environment located" OK

# ---------------------------------------------------------------------
# Preflight: verify uvicorn is importable (with spinner)
# ---------------------------------------------------------------------
$uv = Invoke-WithSpinner `
    -Message    "Checking uvicorn import..." `
    -FilePath   $PythonExe `
    -Arguments  '-c "import uvicorn; print(''OK'')"' `
    -TimeoutSeconds 60

if ($uv.ExitCode -ne 0 -or $uv.StdOut -notmatch 'OK') {
    Write-Status "uvicorn import" FAIL
    Write-Host "[ERROR] uvicorn is not installed in .venv." -ForegroundColor Red
    if ($uv.StdErr) { Write-Host $uv.StdErr -ForegroundColor DarkRed }
    Write-Host "        Run: .\setup.ps1" -ForegroundColor Yellow
    exit 1
}
Write-Status "uvicorn import" OK

# ---------------------------------------------------------------------
# Preflight: locate the `omweb` package and make it importable.
#
# The backend can use a flat layout (backend/omweb), a src layout
# (backend/src/omweb), or an app layout (backend/app/omweb). If the
# package is NOT installed as editable, `python -c "import omweb.main"`
# fails with ModuleNotFoundError even though uvicorn itself is present.
#
# We search the likely locations, add the parent directory to PYTHONPATH,
# and this also covers the final `uvicorn.run("omweb.main:app")` call.
# ---------------------------------------------------------------------
$omwebParent = $null

$omwebCandidates = @(
    (Join-Path $BackendDir "omweb"),
    (Join-Path $BackendDir "src\omweb"),
    (Join-Path $BackendDir "app\omweb")
)
foreach ($cand in $omwebCandidates) {
    if (Test-Path (Join-Path $cand "__init__.py")) {
        $omwebParent = Split-Path -Parent $cand
        break
    }
}

if (-not $omwebParent) {
    $found = Get-ChildItem -Path $BackendDir -Recurse -Directory -Filter "omweb" -ErrorAction SilentlyContinue |
             Where-Object {
                 $_.FullName -notmatch '\\\.venv\\' -and
                 $_.FullName -notmatch '\\node_modules\\' -and
                 (Test-Path (Join-Path $_.FullName "__init__.py"))
             } |
             Select-Object -First 1
    if ($found) { $omwebParent = $found.Parent.FullName }
}

if (-not $omwebParent) {
    Write-Status "Locate omweb package" FAIL
    Write-Host "[ERROR] Could not find the 'omweb' package under:" -ForegroundColor Red
    Write-Host "        $BackendDir" -ForegroundColor Yellow
    Write-Host "        Checked: omweb\  src\omweb\  app\omweb\  and recursively." -ForegroundColor Yellow
    Write-Host "        Ensure the backend sources are present and setup.ps1 finished." -ForegroundColor Yellow
    exit 1
}

# Prepend to PYTHONPATH while preserving any existing value.
$existingPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { "" }
$env:PYTHONPATH = if ($existingPythonPath) { "$omwebParent;$existingPythonPath" } else { $omwebParent }

Write-Status "omweb package located at $omwebParent" OK
Write-Status "PYTHONPATH set to include $omwebParent" OK

# ---------------------------------------------------------------------
# Preflight: verify the ASGI app imports (with spinner - usually the
# slowest step, as it walks the full omweb.* import tree)
# ---------------------------------------------------------------------
$app = Invoke-WithSpinner `
    -Message    "Importing ASGI app (omweb.main)..." `
    -FilePath   $PythonExe `
    -Arguments  '-c "import omweb.main; print(''OK'')"' `
    -TimeoutSeconds 180

if ($app.ExitCode -ne 0 -or $app.StdOut -notmatch 'OK') {
    Write-Status "ASGI app import (omweb.main)" FAIL
    Write-Host "[ERROR] Failed to import omweb.main (ASGI app)." -ForegroundColor Red
    if ($app.StdErr) { Write-Host $app.StdErr -ForegroundColor DarkRed }
    exit 1
}
Write-Status "ASGI app import (omweb.main)" OK

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
Write-Status "PLAYWRIGHT_BROWSERS_PATH pinned to $BrowserDir" OK

# ---------------------------------------------------------------------
# Preflight: warn (do not block) if Playwright browsers are missing
# ---------------------------------------------------------------------
if ($env:PELDRUN_SKIP_BROWSER_CHECK -ne "1") {
    $pwScript = @'
import os
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        print('OK' if os.path.exists(p.chromium.executable_path) else 'MISSING')
except Exception:
    print('MISSING')
'@
    $pw = Invoke-WithSpinner `
        -Message    "Checking Playwright Chromium..." `
        -FilePath   $PythonExe `
        -Arguments  '-' `
        -StandardInput $pwScript `
        -TimeoutSeconds 180

    $pwOk = ($pw.StdOut -match 'OK') -and ($pw.StdOut -notmatch 'MISSING')
    if ($pwOk) {
        Write-Status "Playwright Chromium available" OK
    } else {
        Write-Status "Playwright Chromium not found" WARN
        Write-Host "           Location: $BrowserDir" -ForegroundColor Yellow
        Write-Host "           browser_interactive will be unavailable." -ForegroundColor Yellow
        Write-Host "           Fix: .\setup.ps1" -ForegroundColor Yellow
        Write-Host ""
    }
}

Write-Host ""

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
# Startup banner (gold color to distinguish from the cyan frontend banner)
#
# Note: -ForegroundColor DarkYellow renders a warm gold tone on the
# default Windows console. For a true 24-bit gold, replace
# -ForegroundColor DarkYellow with the ANSI wrapper shown below.
#
#   $gold  = "$([char]27)[38;2;255;215;0m"
#   $reset = "$([char]27)[0m"
#   Write-Host "$gold  █████...$reset"
# ---------------------------------------------------------------------
Write-Host "███████████  ██████████ █████       ██████████   ███████████   █████  █████ ██████   █████" -ForegroundColor DarkYellow
Write-Host "░░███░░░░░███░░███░░░░░█░░███       ░░███░░░░███ ░░███░░░░░███ ░░███  ░░███ ░░██████ ░░███ " -ForegroundColor DarkYellow
Write-Host " ░███    ░███ ░███  █ ░  ░███        ░███   ░░███ ░███    ░███  ░███   ░███  ░███░███ ░███ " -ForegroundColor DarkYellow
Write-Host " ░██████████  ░██████    ░███        ░███    ░███ ░██████████   ░███   ░███  ░███░░███░███ " -ForegroundColor DarkYellow
Write-Host " ░███░░░░░░   ░███░░█    ░███        ░███    ░███ ░███░░░░░███  ░███   ░███  ░███ ░░██████ " -ForegroundColor DarkYellow
Write-Host " ░███         ░███ ░   █ ░███      █ ░███    ███  ░███    ░███  ░███   ░███  ░███  ░░█████ " -ForegroundColor DarkYellow
Write-Host " █████        ██████████ ███████████ ██████████   █████   █████ ░░████████   █████  ░░█████" -ForegroundColor DarkYellow
Write-Host "░░░░░        ░░░░░░░░░░ ░░░░░░░░░░░ ░░░░░░░░░░   ░░░░░   ░░░░░   ░░░░░░░░   ░░░░░    ░░░░░" -ForegroundColor DarkYellow
Write-Host ""

Write-Host ">>> Starting peldrun Web Backend (FastAPI 2.0)..." -ForegroundColor Cyan
Write-Host "    Project root: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "    Backend dir:  $BackendDir" -ForegroundColor DarkGray
Write-Host "    Python:       $PythonExe" -ForegroundColor DarkGray
Write-Host "    Browsers:     $BrowserDir" -ForegroundColor DarkGray
Write-Host "    Endpoint:     http://${BackendHost}:${BackendPort}" -ForegroundColor DarkGray

# ---------------------------------------------------------------------
# Free the target port if occupied
# ---------------------------------------------------------------------
Write-Host ""
Write-Host ">>> Freeing port ${BackendPort} if occupied..." -ForegroundColor Cyan

Get-NetTCPConnection -LocalPort $BackendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

Write-Status "Port ${BackendPort} is free" OK
Write-Host ""

# ---------------------------------------------------------------------
# Launch Uvicorn (blocking - foreground server)
# ---------------------------------------------------------------------
Write-Host ">>> Launching Uvicorn..." -ForegroundColor Green
Write-Host "    Server is live at http://${BackendHost}:${BackendPort}" -ForegroundColor Green
Write-Host "    Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

& $PythonExe -c "import sys, asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy()) if sys.platform == 'win32' else None; uvicorn.run('omweb.main:app', host='$BackendHost', port=$BackendPort, reload=False)"
