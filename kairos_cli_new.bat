@echo off
rem Start the gateway once (if needed), then launch the standard CLI.
rem The caller's current directory remains the CLI workspace.
setlocal EnableExtensions DisableDelayedExpansion

set "KAIROS_ROOT=%~dp0"
set "KAIROS_WORKSPACE=%CD%"

rem Match main.py's existing optional workspace argument. Resolve a relative
rem workspace before changing directories to the source tree for the gateway.
if not "%~1"=="" (
    for %%I in ("%~1") do set "KAIROS_WORKSPACE=%%~fI"
)

rem Read the gateway host/port from the same Config/.env used by the server.
rem Keep usable defaults if Python or configuration lookup is unavailable.
set "KAIROS_GATEWAY_HOST=127.0.0.1"
set "KAIROS_GATEWAY_PORT=8765"
pushd "%KAIROS_ROOT%" >nul
for /f "tokens=1,2" %%A in ('py -c "from kairos.config import Config; print(Config.KAIROS_GATEWAY_HOST(), Config.KAIROS_GATEWAY_PORT())" 2^>nul') do (
    set "KAIROS_GATEWAY_HOST=%%A"
    set "KAIROS_GATEWAY_PORT=%%B"
)
popd >nul

call :gateway_ready
if not errorlevel 1 goto launch_cli

echo [kairos] Gateway is not running; starting it on %KAIROS_GATEWAY_HOST%:%KAIROS_GATEWAY_PORT%...
rem Use a minimized window so gateway logs do not interfere with the CLI prompt.
start "Kairos Gateway" /min /D "%KAIROS_ROOT%" py -m kairos.gateway_main "%KAIROS_WORKSPACE%"

rem Give Uvicorn a moment to bind before handing control to the CLI.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$hostName='%KAIROS_GATEWAY_HOST%'; if ($hostName -eq '0.0.0.0' -or $hostName -eq '::') { $hostName='127.0.0.1' }; $uri='http://' + $hostName + ':%KAIROS_GATEWAY_PORT%/healthz'; $deadline=(Get-Date).AddSeconds(30); while ((Get-Date) -lt $deadline) { try { $response=Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 1; if ($response.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 250 }; exit 1"
if errorlevel 1 echo [kairos] Warning: gateway did not become ready; launching the CLI anyway.

:launch_cli
rem Use the source-tree main.py, but do not change CWD: main.py uses the
rem caller's directory as the workspace when no workspace argument is supplied.
py "%KAIROS_ROOT%main.py" %*
set "KAIROS_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %KAIROS_EXIT_CODE%

:gateway_ready
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$hostName='%KAIROS_GATEWAY_HOST%'; if ($hostName -eq '0.0.0.0' -or $hostName -eq '::') { $hostName='127.0.0.1' }; try { $response=Invoke-WebRequest -UseBasicParsing -Uri ('http://' + $hostName + ':%KAIROS_GATEWAY_PORT%/healthz') -TimeoutSec 1; if ($response.StatusCode -eq 200) { exit 0 } } catch {}; exit 1"
exit /b %ERRORLEVEL%
