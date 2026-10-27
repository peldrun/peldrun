# installer/server.ps1
$ErrorActionPreference = "Stop"

# Force UTF-8 console output
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SetupScript = Join-Path $ProjectRoot "setup.ps1"
$UIPath      = Join-Path $PSScriptRoot "ui.html"
$LogPath     = Join-Path $PSScriptRoot "install.log"
$ErrPath     = Join-Path $PSScriptRoot "install.err"

$StartPort = if ($env:PELDRUN_INSTALLER_PORT) { [int]$env:PELDRUN_INSTALLER_PORT } else { 8765 }

if (-not (Test-Path $SetupScript)) { Write-Host "[FATAL] setup.ps1 not found." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $UIPath)) { Write-Host "[FATAL] ui.html not found." -ForegroundColor Red; exit 1 }

Remove-Item $LogPath, $ErrPath -ErrorAction SilentlyContinue

$script:InstallProcess = $null
$script:InstallStarted = $false

# ---------------------------------------------------------
# Automatically find an available port
# ---------------------------------------------------------
$listener = $null
$Prefix   = $null
$Port     = $StartPort
$MaxPort  = $StartPort + 20
$bound    = $false

while ($Port -le $MaxPort) {
    $tryPrefix = "http://localhost:$Port/"
    $tryListener = $null
    try {
        $tryListener = New-Object System.Net.HttpListener
        $tryListener.Prefixes.Add($tryPrefix)
        $tryListener.Start()
        $listener = $tryListener
        $Prefix   = $tryPrefix
        $bound    = $true
        break
    } catch {
        if ($tryListener) { try { $tryListener.Close() } catch { } }
        $Port++
    }
}

if (-not $bound) {
    Write-Host "[FATAL] Could not bind to any port in range $StartPort to $MaxPort." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PELDRUN Web Installer" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Listening: $Prefix" -ForegroundColor Green
if ($Port -ne $StartPort) {
    Write-Host "  (Port $StartPort was busy, using $Port instead)" -ForegroundColor Yellow
}
Write-Host "  Opening browser..." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Keep this window open during installation." -ForegroundColor Yellow
Write-Host "  Press Ctrl+C when finished." -ForegroundColor Yellow
Write-Host ""

Start-Process $Prefix

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Send-Json {
    param($Response, $Status, $Object)
    $Response.StatusCode = $Status
    $Response.ContentType = "application/json; charset=utf-8"
    $json = $Object | ConvertTo-Json -Compress -Depth 5
    $bytes = $utf8NoBom.GetBytes($json)
    $Response.OutputStream.Write($bytes, 0, $bytes.Length)
}

function Read-LinesFromPosition {
    param([string]$Path, [long]$FromPos)
    $result = @{ lines = @(); newPos = $FromPos }
    if (-not (Test-Path $Path)) { return $result }
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        try {
            if ($FromPos -gt $fs.Length) { $FromPos = 0 }
            $fs.Position = $FromPos
            $sr = New-Object System.IO.StreamReader($fs, $utf8NoBom)
            $collected = New-Object System.Collections.ArrayList
            try {
                while (-not $sr.EndOfStream) {
                    $line = $sr.ReadLine()
                    if ($null -ne $line) { [void]$collected.Add($line) }
                }
            } finally { $sr.Close() }
            $result.lines = $collected.ToArray()
            $result.newPos = $fs.Position
        } finally { $fs.Close() }
    } catch { }
    return $result
}

function Read-AllLines {
    param([string]$Path)
    $result = @()
    if (-not (Test-Path $Path)) { return $result }
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        try {
            $sr = New-Object System.IO.StreamReader($fs, $utf8NoBom)
            $collected = New-Object System.Collections.ArrayList
            try {
                while (-not $sr.EndOfStream) {
                    $line = $sr.ReadLine()
                    if ($null -ne $line) { [void]$collected.Add($line) }
                }
            } finally { $sr.Close() }
            $result.lines = $collected.ToArray()
        } finally { $fs.Close() }
    } catch { }
    return $result
}

try {
    while ($listener.IsListening) {
        $context  = $listener.GetContext()
        $request  = $context.Request
        $response = $context.Response
        $path     = $request.Url.AbsolutePath

        try {
            switch -Exact ($path) {
                "/" {
                    $html = [System.IO.File]::ReadAllText($UIPath, $utf8NoBom)
                    $bytes = $utf8NoBom.GetBytes($html)
                    $response.ContentType = "text/html; charset=utf-8"
                    $response.ContentLength64 = $bytes.Length
                    $response.OutputStream.Write($bytes, 0, $bytes.Length)
                }

                "/favicon.ico" {
                    $response.StatusCode = 204
                }

                "/assets/image/peldrun-logo-light.svg" {
                    $logoPath = Join-Path $ProjectRoot "assets\image\peldrun-logo-light.svg"
                    if (Test-Path $logoPath) {
                        $bytes = [System.IO.File]::ReadAllBytes($logoPath)
                        $response.ContentType = "image/svg+xml"
                        $response.ContentLength64 = $bytes.Length
                        $response.OutputStream.Write($bytes, 0, $bytes.Length)
                    } else {
                        $response.StatusCode = 404
                    }
                }

                "/api/check" {
                    $checkScript = Join-Path $PSScriptRoot "check.ps1"
                    if (Test-Path $checkScript) {
                        $json = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $checkScript
                        $response.StatusCode = 200
                        $response.ContentType = "application/json; charset=utf-8"
                        $bytes = $utf8NoBom.GetBytes($json)
                        $response.OutputStream.Write($bytes, 0, $bytes.Length)
                    } else {
                        Send-Json $response 404 @{ error = "check.ps1 not found" }
                    }
                }

                "/api/status" {
                    $status = "idle"
                    $exitCode = $null
                    if ($script:InstallProcess) {
                        if ($script:InstallProcess.HasExited) {
                            $status = "done"
                            $exitCode = $script:InstallProcess.ExitCode
                        } else {
                            $status = "running"
                        }
                    }
                    Send-Json $response 200 @{
                        status    = $status
                        started   = $script:InstallStarted
                        exit_code = $exitCode
                    }
                }

                "/api/install" {
                    if ($script:InstallProcess -and -not $script:InstallProcess.HasExited) {
                        Send-Json $response 409 @{ error = "Installation already running" }
                    } else {
                        Remove-Item $LogPath, $ErrPath -ErrorAction SilentlyContinue
                        $script:InstallStarted = $true
                        $script:InstallProcess = Start-Process `
                            -FilePath "powershell.exe" `
                            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$SetupScript`"" `
                            -RedirectStandardOutput $LogPath `
                            -RedirectStandardError $ErrPath `
                            -PassThru -NoNewWindow -WorkingDirectory $ProjectRoot
                        Send-Json $response 200 @{ status = "started"; pid = $script:InstallProcess.Id }
                    }
                }

                # ---------------------------------------------------------
                # Polling endpoint — replaces SSE entirely.
                # Returns immediately with any new lines since $since.
                # Browser polls every ~300ms; each call is <50ms.
                # ---------------------------------------------------------
                "/api/output" {
                    $sinceStr = "0"
                    if ($request.Url.Query -match "[?&]since=(\d+)") {
                        $sinceStr = $Matches[1]
                    }
                    $since = [long]$sinceStr

                    $outResult = Read-LinesFromPosition -Path $LogPath -FromPos $since
                    $errLines  = Read-AllLines -Path $ErrPath

                    $allLines = New-Object System.Collections.ArrayList
                    foreach ($line in $outResult.lines) {
                        [void]$allLines.Add(@{ type = "stdout"; data = $line })
                    }
                    foreach ($line in $errLines) {
                        [void]$allLines.Add(@{ type = "stderr"; data = $line })
                    }

                    $done = $false
                    $exitCode = $null
                    if ($script:InstallProcess) {
                        $script:InstallProcess.Refresh()
                        if ($script:InstallProcess.HasExited) {
                            $done = $true
                            $exitCode = $script:InstallProcess.ExitCode
                            # PowerShell 5.1 sometimes returns null even on success
                            if ($null -eq $exitCode) { $exitCode = 0 }
                        }
                    }

                    Send-Json $response 200 @{
                        lines     = @($allLines.ToArray())
                        nextPos   = $outResult.newPos
                        done      = [bool]$done
                        exitCode  = $exitCode
                        started   = $script:InstallStarted
                    }
                }

                default {
                    $response.StatusCode = 404
                    $msg = $utf8NoBom.GetBytes('{"error":"Not found"}')
                    $response.ContentType = "application/json"
                    $response.OutputStream.Write($msg, 0, $msg.Length)
                }
            }
        } catch {
            Write-Host "[ERROR] Handler: $_" -ForegroundColor Red
            try { $response.StatusCode = 500 } catch { }
        } finally {
            try { $response.Close() } catch { }
        }
    }
} finally {
    try { $listener.Stop(); $listener.Close() } catch { }
}
