Write-Host ">>> Starting peldrun Web Backend (FastAPI 2.0)..." -ForegroundColor Cyan
Set-Location -Path "D:\AI\peldrun\backend"

$venvPython = "D:\AI\peldrun\backend\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[ERROR] Virtual environment not found at: $venvPython" -ForegroundColor Red
    exit 1
}

# Free port 8088 if occupied
Get-NetTCPConnection -LocalPort 8088 -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -gt 0 } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

Write-Host ">>> Launching Uvicorn with Windows Proactor loop on http://localhost:8088..." -ForegroundColor Green
& $venvPython -c "import sys, asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy()) if sys.platform == 'win32' else None; uvicorn.run('omweb.main:app', host='0.0.0.0', port=8088, reload=False)"
