@echo off
setlocal
title Aria FSMCoT Compact Proxy

set "SCRIPT_DIR=%~dp0"
set "UPSTREAM=http://127.0.0.1:1918"
set "PORT=1921"

if not "%FSMCOT_UPSTREAM%"=="" set "UPSTREAM=%FSMCOT_UPSTREAM%"
if not "%FSMCOT_PORT%"=="" set "PORT=%FSMCOT_PORT%"

cd /d "%SCRIPT_DIR%"
echo ============================================================================
echo  Aria FSMCoT Compact - Standalone Zero-Dependency OpenAI Proxy
echo  Listen   : http://127.0.0.1:%PORT%
echo  Upstream : %UPSTREAM%
echo ============================================================================
python "%SCRIPT_DIR%fsmcot_gateway.py" --host 127.0.0.1 --port %PORT% --upstream "%UPSTREAM%" %*
if errorlevel 1 goto RUN_ERROR
exit /b 0

:RUN_ERROR
echo [ERROR] Proxy exited with errorlevel %errorlevel%
pause
exit /b 1
