# installer/check.ps1
$ErrorActionPreference = "SilentlyContinue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $ProjectRoot "backend"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$BrowserDir = Join-Path $ProjectRoot ".playwright-browsers"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"

$result = @{
    config = $false
    python = $false; pythonVersion = ""
    venv = $false
    backendDeps = $false
    keyring = $false
    playwright = $false
    appImport = $false
    node = $false; nodeVersion = ""
    frontendDeps = $false
}

$result.config = Test-Path (Join-Path $ProjectRoot "config\config.toml")

foreach ($cmd in @("python", "python3", "py")) {
    try {
        $out = & $cmd --version 2>&1
        if ($out -match "Python (\d+)\.(\d+)\.(\d+)") {
            $result.pythonVersion = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 12) {
                $result.python = $true
                break
            }
        }
    } catch {}
}

$result.venv = Test-Path $VenvPython

if ($result.venv) {
    $null = & $VenvPython -c "import uvicorn" 2>$null
    if ($LASTEXITCODE -eq 0) { $result.backendDeps = $true }

    $null = & $VenvPython -c "import keyring" 2>$null
    if ($LASTEXITCODE -eq 0) { $result.keyring = $true }

    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserDir
    $pwCheck = "import os`ntry:`n    from playwright.sync_api import sync_playwright`n    with sync_playwright() as p:`n        print('OK' if os.path.exists(p.chromium.executable_path) else 'MISSING')`nexcept Exception:`n    print('MISSING')"
    $pw = & $VenvPython -c $pwCheck 2>$null | Select-Object -Last 1
    if ($pw -eq "OK") { $result.playwright = $true }

    $null = & $VenvPython -c "import omweb.main" 2>$null
    if ($LASTEXITCODE -eq 0) { $result.appImport = $true }
}

foreach ($cmd in @("node", "nodejs")) {
    try {
        $out = & $cmd --version 2>&1
        if ($out -match "v(\d+)\.(\d+)\.(\d+)") {
            $result.nodeVersion = "v$($Matches[1]).$($Matches[2]).$($Matches[3])"
            if ([int]$Matches[1] -gt 20 -or ([int]$Matches[1] -eq 20 -and [int]$Matches[2] -ge 9)) {
                $result.node = $true
                break
            }
        }
    } catch {}
}

$result.frontendDeps = Test-Path (Join-Path $FrontendDir "node_modules\next")

$result | ConvertTo-Json -Compress
