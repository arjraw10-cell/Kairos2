@echo off
rem Kairos CLI without the gateway: the agent runs in this process.
rem Keeps the caller's current folder and passes all arguments through.
py "%~dp0main.py" --offline %*
