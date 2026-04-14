@echo off
setlocal
cd /d "%~dp0"
title CLIProxyAPI-Auto-Disable-Enable

if "%CLIPROXYAPI_MANAGEMENT_KEY%"=="" (
    echo [WARNING] CLIPROXYAPI_MANAGEMENT_KEY environment variable is not set!
    echo Please set it to your CLIProxyAPI management password.
    echo.
    set /p CLIPROXYAPI_MANAGEMENT_KEY="Enter management key: "
)

echo ========================================
echo  CLIProxyAPI-Auto-Disable-Enable
echo  Web UI: http://127.0.0.1:8320
echo ========================================
echo.

REM Independent deployment: uncomment and modify the lines below
REM set CLIPROXYAPI_CONFIG=D:\CLIProxyAPI\config.yaml
REM set CLIPROXYAPI_AUTH_DIR=D:\CLIProxyAPI\data

if defined CLIPROXYAPI_CONFIG (
    python -u account_monitor_web.py --port 8320 --config %CLIPROXYAPI_CONFIG% --auth-dir %CLIPROXYAPI_AUTH_DIR%
) else (
    python -u account_monitor_web.py --port 8320
)

echo.
echo Monitor exited.
pause
