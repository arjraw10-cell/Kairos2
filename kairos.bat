@echo off
rem Kairos CLI connected to the gateway. Starts the gateway in a minimized
rem window when /healthz does not answer. Keeps the caller's current folder
rem and passes all arguments through (kairos . / kairos <path>).
setlocal EnableExtensions DisableDelayedExpansion

set "KAIROS_ROOT=%~dp0"
if not defined KAIROS_GATEWAY_HOST set "KAIROS_GATEWAY_HOST=127.0.0.1"
if not defined KAIROS_GATEWAY_PORT set "KAIROS_GATEWAY_PORT=8765"
set "KAIROS_HEALTH_HOST=%KAIROS_GATEWAY_HOST%"
if "%KAIROS_HEALTH_HOST%"=="0.0.0.0" set "KAIROS_HEALTH_HOST=127.0.0.1"
if "%KAIROS_HEALTH_HOST%"=="::" set "KAIROS_HEALTH_HOST=127.0.0.1"
set "KAIROS_HEALTH_URL=http://%KAIROS_HEALTH_HOST%:%KAIROS_GATEWAY_PORT%/healthz"

rem curl.exe ships with Windows 10+ and answers in ~60 ms (PowerShell: ~350 ms).
curl.exe -sf -o NUL --connect-timeout 1 "%KAIROS_HEALTH_URL%" >NUL 2>&1
if not errorlevel 1 goto launch_cli

echo [kairos] Starting the gateway...
rem cmd /k keeps the window (and the error) when the gateway exits.
start "Kairos Gateway" /min /D "%KAIROS_ROOT%" cmd /k py -m kairos.gateway_main
curl.exe -sf -o NUL --connect-timeout 1 --retry 30 --retry-delay 1 --retry-connrefused --retry-max-time 40 "%KAIROS_HEALTH_URL%" >NUL 2>&1
if errorlevel 1 echo [kairos] The gateway did not start; see the "Kairos Gateway" window.

:launch_cli
py "%KAIROS_ROOT%main.py" %*
set "KAIROS_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %KAIROS_EXIT_CODE%
