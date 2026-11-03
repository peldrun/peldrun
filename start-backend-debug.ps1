# start-backend-debug.ps1
#
# Fast DEBUG launcher for PELDRUN Web Backend.
#
# Differences from start-backend.ps1:
#   1. Skips ALL preflight checks (uvicorn import, ASGI import,
#      Playwright check) → instant startup.
#   2. Forces DEBUG log level on root + omweb.* + peldrun.* + SDKs.
#   3. Enables uvicorn access_log so every HTTP request appears.
#   4. Sets PYTHONUNBUFFERED so log lines appear immediately.
#
# Use this when actively debugging. Use start-backend.ps1 for
# production / first-time startup.

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

$ProjectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$BackendDir  = Join-Path $ProjectRoot "backend"

if (-not (Test-Path $BackendDir)) {
    Write-Host "[ERROR] Backend directory not found: $BackendDir" -ForegroundColor Red
    exit 1
}

Set-Location -Path $BackendDir

# ---------------------------------------------------------------------
# Minimal checks — Python + omweb only (no import probes)
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
    Write-Host "[ERROR] Virtual environment not found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

$omwebParent = $null
foreach ($cand in @(
    (Join-Path $BackendDir "omweb"),
    (Join-Path $BackendDir "src\omweb"),
    (Join-Path $BackendDir "app\omweb")
)) {
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
    Write-Host "[ERROR] omweb package not found under $BackendDir" -ForegroundColor Red
    exit 1
}

$existingPythonPath = if ($env:PYTHONPATH) { $env:PYTHONPATH } else { "" }
$env:PYTHONPATH = if ($existingPythonPath) { "$omwebParent;$existingPythonPath" } else { $omwebParent }

# ---------------------------------------------------------------------
# Playwright — set path but skip the availability check
# ---------------------------------------------------------------------
$BrowserDir = Join-Path $ProjectRoot ".playwright-browsers"
if (-not (Test-Path $BrowserDir)) { New-Item -ItemType Directory -Path $BrowserDir | Out-Null }
$env:PLAYWRIGHT_BROWSERS_PATH  = $BrowserDir
$env:PELDRUN_SKIP_BROWSER_CHECK = "1"

# ---------------------------------------------------------------------
# DEBUG logging env vars (belt + suspenders)
# ---------------------------------------------------------------------
$env:LOG_LEVEL         = "DEBUG"
$env:PELDRUN_LOG_LEVEL = "DEBUG"
$env:PELDRUN_DEBUG     = "1"
$env:PYTHONLOGLEVEL    = "DEBUG"
$env:PYTHONUNBUFFERED  = "1"

# ---------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------
$BackendHost = if ($env:PELDRUN_BACKEND_HOST) { $env:PELDRUN_BACKEND_HOST } else { "0.0.0.0" }
$BackendPort = if ($env:PELDRUN_BACKEND_PORT) { [int]$env:PELDRUN_BACKEND_PORT } else { 8088 }

# Free port if occupied (fast, silent)
Get-NetTCPConnection -LocalPort $BackendPort -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host ">>> DEBUG mode - fast startup (preflight skipped)" -ForegroundColor Yellow
Write-Host "    Python:   $PythonExe" -ForegroundColor DarkGray
Write-Host "    Endpoint: http://${BackendHost}:${BackendPort}" -ForegroundColor DarkGray
Write-Host "    Log level: DEBUG (root + omweb.* + peldrun.* + httpx + google)" -ForegroundColor Yellow
Write-Host ""

# ---------------------------------------------------------------------
# Bootstrap script — a temp .py file is used so we can configure
# logging AFTER importing omweb.main (in case it calls
# logging.basicConfig itself) and force DEBUG on every logger.
# ---------------------------------------------------------------------
$tmpDir = if ($env:TEMP) { $env:TEMP } elseif ($env:TMPDIR) { $env:TMPDIR } else { "/tmp" }
$bootstrapFile = Join-Path $tmpDir "peldrun-debug-bootstrap.py"

$bootstrap = @"
import asyncio, logging, sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 1. Configure the root logger FIRST (force=True overrides any prior call).
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    force=True,
)

# 2. Import the ASGI app. This may reconfigure logging internally;
#    we re-apply DEBUG afterwards.
import omweb.main as _main_app

# 3. Force DEBUG on the loggers we care about.
logging.getLogger().setLevel(logging.DEBUG)
for _name in (
    'omweb',
    'peldrun',
    'omweb.routers.config_rtr',
    'omweb.agent_bridge_parts',
    'peldrun.llm.providers.google_genai',
    'peldrun.llm.providers.factory',
    'peldrun.llm.catalog',
    'peldrun.llm.client',
    'peldrun.runtime',
    'httpx',
    'httpcore',
    'google',
    'google.genai',
    'openai',
):
    logging.getLogger(_name).setLevel(logging.DEBUG)

# 4. Make sure no handler filters hide DEBUG records.
for _h in logging.getLogger().handlers:
    _h.setLevel(logging.DEBUG)

# 5. Launch uvicorn with the app object (not the string form) so our
#    logging config survives into the server.
import uvicorn
uvicorn.run(
    _main_app.app,
    host='$BackendHost',
    port=$BackendPort,
    reload=False,
    log_level='debug',
    access_log=True,
)
"@

Set-Content -Path $bootstrapFile -Value $bootstrap -Encoding UTF8

& $PythonExe $bootstrapFile
