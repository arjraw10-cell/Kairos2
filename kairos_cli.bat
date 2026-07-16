@echo off
setlocal
setlocal enabledelayedexpansion

set "ROOT=%~dp0"

:: ── Check if gateway is already listening on port 8765 ──
set "GATEWAY_RUNNING=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R ":8765.*LISTENING" 2^>nul') do (
    set "GATEWAY_RUNNING=1"
)

if "!GATEWAY_RUNNING!"=="1" (
    echo Gateway is already running.
    goto :launch_cli
)

echo Starting Kairos Gateway...
start "KairosGateway" /min cmd /c "cd /d %ROOT% && python -m kairos.main_gateway"

echo Waiting for gateway to be ready...
:wait_loop
timeout /t 1 /nobreak >nul
set "LISTENING=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R ":8765.*LISTENING" 2^>nul') do (
    set "LISTENING=1"
)
if "!LISTENING!"=="0" goto :wait_loop
echo Gateway ready.

:launch_cli
echo Launching Kairos CLI...
cd /d "%ROOT%"
python -m kairos.main 

echo CLI exited.
