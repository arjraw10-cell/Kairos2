@echo off
setlocal

rem Emergency fallback: run the pre-gateway CLI that owns the Agent runtime.
rem The snapshot is kept on backup/main-before-gateway and is materialized
rem temporarily so this launcher does not duplicate the legacy source tree.
set "ROOT=%~dp0"
set "LEGACY_ROOT=%TEMP%\kairos_old_g_%RANDOM%_%RANDOM%"

pushd "%ROOT%"
git worktree add --detach "%LEGACY_ROOT%" backup/main-before-gateway >nul 2>&1
if errorlevel 1 (
    popd
    echo Failed to materialize backup/main-before-gateway.
    echo Verify that the backup branch exists with: git branch --list backup/main-before-gateway
    exit /b 1
)
popd

rem Do not change directories: the caller's CWD remains the workspace context.
py "%LEGACY_ROOT%\main.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

pushd "%ROOT%"
git worktree remove --force "%LEGACY_ROOT%" >nul 2>&1
popd

exit /b %EXIT_CODE%
