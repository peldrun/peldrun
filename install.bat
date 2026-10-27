@echo off
REM ============================================================
REM  PELDRUN Web Installer — Double-click to start
REM ============================================================
REM  Starts a local HTTP server that serves a beautiful installer
REM  UI and streams setup.ps1 output live to the browser.
REM ============================================================

cd /d "%~dp0"

if not exist "installer\server.ps1" (
    echo.
    echo [ERROR] installer\server.ps1 not found.
    echo         Expected next to this file.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting PELDRUN installer server...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "installer\server.ps1"

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Installer exited with code %ERRORLEVEL%.
    pause
)
