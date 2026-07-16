@echo off
rem Run the legacy CLI from this batch file's source tree while preserving the caller's workspace CWD.
py "%~dp0main.py" %*
