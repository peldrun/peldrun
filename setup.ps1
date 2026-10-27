# setup.ps1 — One-time project initialization for PELDRUN Web
#
# Run once after cloning/extracting the repository:
#     .\setup.ps1
#
# Then run:
#     .\start-backend.ps1
#
# Idempotent: safe to re-run.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PELDRUN Web - First-time Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Steps:" -ForegroundColor White
Write-Host "    1/7  Verify config\config.toml exists" -ForegroundColor Gray
Write-Host "    2/7  Ensure Python 3.12+ (auto-install if missing)" -ForegroundColor Gray
Write-Host "    3/7  Create backend\.venv" -ForegroundColor Gray
Write-Host "    4/7  Install Python dependencies (requirements.txt)" -ForegroundColor Gray
Write-Host "    5/7  Install keyring for Windows Credential Manager" -ForegroundColor Gray
Write-Host "    6/7  Install Playwright Chromium (project-local, forced)" -ForegroundColor Gray
Write-Host "    7/7  Verify omweb.main imports" -ForegroundColor Gray
Write-Host ""

$ProjectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$BackendDir  = Join-Path $ProjectRoot "backend"

if (-not (Test-Path $BackendDir)) {
    Write-Host "[FATAL] backend\ not found at: $BackendDir" -ForegroundColor Red
    exit 1
}

# ---------- helpers ----------

function Test-Python312Plus {
    param([string]$Path)
    try {
        $out = & $Path --version 2>&1
        if ($out -match "Python (\d+)\.(\d+)") {
            return ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 12)
        }
    } catch { }
    return $false
}

function Find-PythonExecutable {
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $out = & $cmd --version 2>&1
            if ($out -match "Python (\d+)\.(\d+)") {
                if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 12) {
                    $resolved = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
                    if ($resolved -and (Test-Path $resolved)) { return $resolved }
                }
            }
        } catch { }
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python313\python.exe"
    )
    foreach ($c in $candidates) {
        if ((Test-Path $c) -and (Test-Python312Plus $c)) { return $c }
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    Trying winget (Python.Python.3.12)..." -ForegroundColor Yellow
        try {
            & winget install --id Python.Python.3.12 --silent `
                --accept-package-agreements --accept-source-agreements 2>&1 | Out-Host
            Start-Sleep -Seconds 2
            $found = Find-PythonExecutable
            if ($found) { return $found }
        } catch {
            Write-Host "    winget failed: $_" -ForegroundColor Yellow
        }
    } else {
        Write-Host "    winget not available; using python.org download." -ForegroundColor Yellow
    }

    foreach ($v in @("3.12.8", "3.12.7", "3.12.6")) {
        $url = "https://www.python.org/ftp/python/$v/python-$v-amd64.exe"
        try {
            $head = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing -TimeoutSec 10
            if ($head.StatusCode -ne 200) { continue }
        } catch { continue }

        $installer = Join-Path $env:TEMP "python-$v-installer.exe"
        Write-Host "    Downloading Python $v..." -ForegroundColor Yellow
        try {
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
        } catch { continue }

        Write-Host "    Installing Python $v (silent, user-scope)..." -ForegroundColor Yellow
        $argStr = "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1 Include_launcher=1"
        try {
            $proc = Start-Process -FilePath $installer -ArgumentList $argStr -Wait -PassThru
            if ($proc.ExitCode -ne 0) { continue }
        } catch { continue } finally {
            Remove-Item $installer -ErrorAction SilentlyContinue
        }

        Start-Sleep -Seconds 2
        $found = Find-PythonExecutable
        if ($found) { return $found }
    }
    return $null
}

# ========== [1/7] config.toml check ==========
Write-Host ""
Write-Host ">>> [1/7] Verifying config\config.toml..." -ForegroundColor Cyan
$ConfigFile = Join-Path $ProjectRoot "config\config.toml"
if (-not (Test-Path $ConfigFile)) {
    Write-Host "[FATAL] config\config.toml not found." -ForegroundColor Red
    Write-Host "        Expected: $ConfigFile" -ForegroundColor Yellow
    exit 1
}
Write-Host "    Found (will NOT be modified)." -ForegroundColor Green

# ========== [2/7] Python 3.12+ ==========
Write-Host ""
Write-Host ">>> [2/7] Ensuring Python 3.12+..." -ForegroundColor Cyan
$PythonExe = Find-PythonExecutable
if ($PythonExe) {
    $ver = & $PythonExe --version 2>&1
    Write-Host "    Found: $ver at $PythonExe" -ForegroundColor Green
} else {
    Write-Host "    Python 3.12+ not detected. Installing automatically..." -ForegroundColor Yellow
    $PythonExe = Install-Python
    if (-not $PythonExe) {
        Write-Host "[FATAL] Could not install Python 3.12+ automatically." -ForegroundColor Red
        Write-Host "        Install manually: https://www.python.org/downloads/" -ForegroundColor Yellow
        exit 1
    }
    $ver = & $PythonExe --version 2>&1
    Write-Host "    Installed: $ver at $PythonExe" -ForegroundColor Green
}

# ========== [3/7] .venv ==========
Write-Host ""
Write-Host ">>> [3/7] Ensuring backend\.venv..." -ForegroundColor Cyan
Set-Location $BackendDir
$VenvDir = Join-Path $BackendDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Write-Host "    Creating .venv..." -ForegroundColor Yellow
    & $PythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FATAL] venv creation failed." -ForegroundColor Red
        exit 1
    }
    Write-Host "    Created." -ForegroundColor Green
} else {
    Write-Host "    Already exists." -ForegroundColor Green
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[FATAL] .venv\Scripts\python.exe missing." -ForegroundColor Red
    exit 1
}

# ========== [4/7] requirements.txt ==========
Write-Host ""
Write-Host ">>> [4/7] Installing Python dependencies..." -ForegroundColor Cyan
$ReqFile = Join-Path $BackendDir "requirements.txt"
if (-not (Test-Path $ReqFile)) {
    Write-Host "[FATAL] requirements.txt not found." -ForegroundColor Red
    exit 1
}
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FATAL] Dependency installation failed." -ForegroundColor Red
    exit 1
}

# ========== [5/7] keyring (Windows Credential Manager) ==========
Write-Host ""
Write-Host ">>> [5/7] Windows Credential Manager (keyring)..." -ForegroundColor Cyan
if ($env:OS -eq "Windows_NT") {
    # Run Python import checks without letting stderr abort the script.
    # PowerShell 5.1 treats stderr from native commands as terminating
    # errors when ErrorActionPreference = "Stop".
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    $keyringOut = & $VenvPython -c "import keyring; print('OK')" 2>&1 | Out-String
    $keyringOk = ($LASTEXITCODE -eq 0) -and ($keyringOut -match "OK")

    $ErrorActionPreference = $prevEAP

    if ($keyringOk) {
        Write-Host "    keyring already installed." -ForegroundColor Green
    } else {
        Write-Host "    Installing keyring + pywin32-ctypes..." -ForegroundColor Yellow
        & $VenvPython -m pip install --quiet "keyring>=25.0.0" "pywin32-ctypes>=0.2.0"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "    [WARN] keyring install failed; vault will fall back to EncryptedFileVault." -ForegroundColor Yellow
        } else {
            Write-Host "    Installed." -ForegroundColor Green
        }
    }

    # Verify the active backend (non-fatal if this fails)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $backend = & $VenvPython -c "import keyring; print(keyring.get_keyring().__class__.__name__)" 2>&1 | Out-String
    $backendOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP

    if ($backendOk) {
        Write-Host "    Active backend: $($backend.Trim())" -ForegroundColor Green
    } else {
        Write-Host "    [WARN] Could not determine keyring backend; using EncryptedFileVault." -ForegroundColor Yellow
    }
} else {
    Write-Host "    Skipped (non-Windows OS)." -ForegroundColor DarkGray
}

# ========== [6/7] Playwright Chromium (PROJECT-LOCAL, FORCED) ==========
Write-Host ""
Write-Host ">>> [6/7] Installing Playwright Chromium (project-local, forced)..." -ForegroundColor Cyan

# Force project-local browser directory. ALWAYS overrides any inherited
# PLAYWRIGHT_BROWSERS_PATH so this install remains fully self-contained.
$BrowserDir = Join-Path $ProjectRoot ".playwright-browsers"
if (-not (Test-Path $BrowserDir)) {
    New-Item -ItemType Directory -Path $BrowserDir | Out-Null
    Write-Host "    Created: $BrowserDir" -ForegroundColor DarkGray
}

$previous = $env:PLAYWRIGHT_BROWSERS_PATH
if ($previous -and $previous -ne $BrowserDir) {
    Write-Host "    [INFO] Ignoring inherited PLAYWRIGHT_BROWSERS_PATH=$previous" -ForegroundColor DarkGray
    Write-Host "           Using project-local path instead." -ForegroundColor DarkGray
}

$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserDir

$pwCheck = @"
import os
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        print('OK' if os.path.exists(p.chromium.executable_path) else 'MISSING')
except Exception:
    print('MISSING')
"@
$pw = & $VenvPython -c $pwCheck 2>$null | Select-Object -Last 1
if ($pw -eq "OK") {
    Write-Host "    Already installed at: $BrowserDir" -ForegroundColor Green
} else {
    Write-Host "    Downloading Chromium + FFmpeg + Headless Shell (~315 MB)..." -ForegroundColor Yellow
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    [WARN] Chromium install failed." -ForegroundColor Yellow
        Write-Host "           Retry: `$env:PLAYWRIGHT_BROWSERS_PATH='$BrowserDir'; .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Yellow
    } else {
        Write-Host "    Installed to: $BrowserDir" -ForegroundColor Green
    }
}

# Ensure .gitignore excludes the browser directory.
$GitIgnore = Join-Path $ProjectRoot ".gitignore"
if (Test-Path $GitIgnore) {
    $content = Get-Content $GitIgnore -Raw
    if ($content -notmatch "(?m)^\.playwright-browsers") {
        Add-Content -Path $GitIgnore -Value "`n# Playwright browsers (project-local, recreated by setup.ps1)`n.playwright-browsers/"
        Write-Host "    Added '.playwright-browsers/' to .gitignore" -ForegroundColor DarkGray
    }
}

# ========== [7/7] omweb.main import verification ==========
Write-Host ""
Write-Host ">>> [7/7] Verifying omweb.main imports..." -ForegroundColor Cyan
$out = & $VenvPython -c "import omweb.main; print('OK')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FATAL] omweb.main import failed." -ForegroundColor Red
    Write-Host $out -ForegroundColor DarkRed
    exit 1
}
Write-Host "    omweb.main imports cleanly." -ForegroundColor Green


# ========== [8/9] Node.js 20.9+ ==========
Write-Host ""
Write-Host ">>> [8/9] Ensuring Node.js 20.9+ (for frontend)..." -ForegroundColor Cyan

function Test-NodeVersion {
    param([string]$Path)
    try {
        $out = & $Path --version 2>&1
        if ($out -match "v(\d+)\.(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            return ($major -gt 20 -or ($major -eq 20 -and $minor -ge 9))
        }
    } catch { }
    return $false
}

function Find-NodeExecutable {
    foreach ($cmd in @("node", "nodejs")) {
        try {
            $resolved = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
            if ($resolved -and (Test-Path $resolved) -and (Test-NodeVersion $resolved)) {
                return $resolved
            }
        } catch { }
    }
    $candidates = @(
        "$env:ProgramFiles\nodejs\node.exe",
        "$env:LOCALAPPDATA\Programs\nodejs\node.exe"
    )
    foreach ($c in $candidates) {
        if ((Test-Path $c) -and (Test-NodeVersion $c)) { return $c }
    }
    return $null
}

function Install-NodeJS {
    # Strategy 1: winget (preferred — always gets latest LTS)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    Trying winget (OpenJS.NodeJS.LTS)..." -ForegroundColor Yellow
        try {
            & winget install --id OpenJS.NodeJS.LTS --silent `
                --accept-package-agreements --accept-source-agreements 2>&1 | Out-Host
            Start-Sleep -Seconds 3
            $found = Find-NodeExecutable
            if ($found) { return $found }
        } catch {
            Write-Host "    winget failed: $_" -ForegroundColor Yellow
        }
    } else {
        Write-Host "    winget not available; using nodejs.org download." -ForegroundColor Yellow
    }

    # Strategy 2: direct MSI download
    $nodeVersion = "v22.11.0"
    $msiName = "node-$nodeVersion-x64.msi"
    $url = "https://nodejs.org/dist/$nodeVersion/$msiName"
    $installer = Join-Path $env:TEMP $msiName

    Write-Host "    Downloading Node.js $nodeVersion..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    } catch {
        Write-Host "    Download failed: $_" -ForegroundColor Yellow
        return $null
    }

    Write-Host "    Installing Node.js $nodeVersion (silent)..." -ForegroundColor Yellow
    try {
        $proc = Start-Process -FilePath "msiexec.exe" `
            -ArgumentList "/i `"$installer`" /quiet /norestart" `
            -Wait -PassThru
        # 0 = success, 3010 = success with reboot pending
        if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
            Write-Host "    Installer exit code: $($proc.ExitCode)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "    Installer launch failed: $_" -ForegroundColor Yellow
    } finally {
        Remove-Item $installer -ErrorAction SilentlyContinue
    }

    Start-Sleep -Seconds 3
    return Find-NodeExecutable
}

$NodeExe = Find-NodeExecutable
if ($NodeExe) {
    $nodeVer = & $NodeExe --version 2>&1
    Write-Host "    Found: Node.js $nodeVer at $NodeExe" -ForegroundColor Green
} else {
    Write-Host "    Node.js 20.9+ not detected. Installing automatically..." -ForegroundColor Yellow
    $NodeExe = Install-NodeJS
    if (-not $NodeExe) {
        Write-Host "[FATAL] Could not install Node.js 20.9+ automatically." -ForegroundColor Red
        Write-Host "        Install manually: https://nodejs.org/en/download/" -ForegroundColor Yellow
        Write-Host "        Next.js 16 requires Node.js >= 20.9.0." -ForegroundColor Yellow
        exit 1
    }
    $nodeVer = & $NodeExe --version 2>&1
    Write-Host "    Installed: Node.js $nodeVer at $NodeExe" -ForegroundColor Green
}

# ========== [9/9] Frontend npm install ==========
Write-Host ""
Write-Host ">>> [9/9] Installing frontend dependencies (npm ci)..." -ForegroundColor Cyan

$FrontendDir = Join-Path $ProjectRoot "frontend"
if (-not (Test-Path $FrontendDir)) {
    Write-Host "[FATAL] frontend\ not found at: $FrontendDir" -ForegroundColor Red
    exit 1
}

$PackageJson = Join-Path $FrontendDir "package.json"
$PackageLock = Join-Path $FrontendDir "package-lock.json"
if (-not (Test-Path $PackageJson)) {
    Write-Host "[FATAL] frontend\package.json not found." -ForegroundColor Red
    exit 1
}

# Locate npm.cmd next to node.exe (standard Node.js MSI layout)
$NodeDir = Split-Path -Parent $NodeExe
$NpmCmd = Join-Path $NodeDir "npm.cmd"
if (-not (Test-Path $NpmCmd)) {
    $resolved = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if ($resolved) { $NpmCmd = $resolved }
    else {
        Write-Host "[FATAL] npm.cmd not found alongside node.exe or in PATH." -ForegroundColor Red
        exit 1
    }
}

$npmVer = & $NpmCmd --version 2>&1
Write-Host "    npm version: $npmVer" -ForegroundColor DarkGray
Write-Host "    Working dir: $FrontendDir" -ForegroundColor DarkGray

Push-Location $FrontendDir
try {
    if (Test-Path $PackageLock) {
        Write-Host "    Running: npm ci (~500 MB, 2-5 min)..." -ForegroundColor Yellow
        & $NpmCmd ci
        if ($LASTEXITCODE -ne 0) {
            Write-Host "    npm ci failed (lockfile may be out of sync); retrying with npm install..." -ForegroundColor Yellow
            & $NpmCmd install
            if ($LASTEXITCODE -ne 0) {
                Write-Host "[FATAL] npm install also failed." -ForegroundColor Red
                Write-Host "        Try manually: cd frontend; npm install" -ForegroundColor Yellow
                exit 1
            }
        }
    } else {
        Write-Host "    package-lock.json missing; running: npm install..." -ForegroundColor Yellow
        & $NpmCmd install
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[FATAL] npm install failed." -ForegroundColor Red
            exit 1
        }
    }

    # Sanity check: Next.js must be present
    if (-not (Test-Path (Join-Path $FrontendDir "node_modules\next"))) {
        Write-Host "[FATAL] node_modules\next missing after install." -ForegroundColor Red
        exit 1
    }
    Write-Host "    Frontend dependencies installed." -ForegroundColor Green
} finally {
    Pop-Location
}

# ========== Done ==========
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Backend:" -ForegroundColor White
Write-Host "    Python:   $VenvPython" -ForegroundColor DarkGray
Write-Host "    Browsers: $BrowserDir" -ForegroundColor DarkGray
Write-Host "    Config:   $ConfigFile" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Frontend:" -ForegroundColor White
Write-Host "    Node.js:  $NodeExe" -ForegroundColor DarkGray
Write-Host "    npm:      $NpmCmd" -ForegroundColor DarkGray
Write-Host "    Modules:  $FrontendDir\node_modules" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Cyan
Write-Host "    Backend:    .\start-backend.ps1" -ForegroundColor White
Write-Host "    Frontend:   .\start-frontend.ps1" -ForegroundColor White
Write-Host "    Both:       .\start-all.ps1" -ForegroundColor White
Write-Host ""
Write-Host ""
