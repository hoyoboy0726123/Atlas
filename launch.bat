@echo off
setlocal
title Atlas Launcher
REM Atlas one-click launcher (backend 8014 / frontend 3012).
REM First run creates the Python environment and installs dependencies;
REM later runs just start the two services.
REM
REM NOTE: keep this file pure ASCII. cmd.exe parses .bat files byte-by-byte in
REM the system ANSI codepage; non-ASCII characters corrupt the whole line.
REM Error checks use "if errorlevel" + goto on purpose: a %VAR% set inside a
REM parenthesized block is expanded before the block runs, so it is always stale.

echo ==================================================
echo  Atlas  (backend 8014 / frontend 3012)
echo ==================================================
echo.

REM ---- 1. Backend environment -----------------------
if exist "%~dp0backend\.venv\Scripts\python.exe" goto env_ready

where uv >NUL 2>&1
if errorlevel 1 goto pip_setup
echo [Setup] Installing backend dependencies with uv (a few minutes on first run)...
pushd "%~dp0backend"
uv sync
if errorlevel 1 goto uv_failed
popd
goto env_ready

:uv_failed
popd
echo [X] uv sync failed. See the errors above.
pause
exit /b 1

:pip_setup
REM No uv: venv + pip. Prefer 3.13, then 3.12, then 3.11 -
REM pydantic 2.9.2 has no wheel for Python 3.14.
set "PY_CMD="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY_CMD (
        py -%%V -c "import sys" >NUL 2>&1
        if not errorlevel 1 set "PY_CMD=py -%%V"
    )
)
if not defined PY_CMD set "PY_CMD=python"
echo [Setup] Creating backend virtual environment using: %PY_CMD%
%PY_CMD% -m venv "%~dp0backend\.venv"
if errorlevel 1 goto venv_failed
echo [Setup] Installing backend dependencies (a few minutes on first run)...
"%~dp0backend\.venv\Scripts\python.exe" -m pip install -q -r "%~dp0backend\requirements.txt"
if errorlevel 1 goto pip_failed
goto env_ready

:venv_failed
echo [X] Could not create the environment. Install Python 3.11-3.13 (or uv) and put it on PATH.
pause
exit /b 1

:pip_failed
echo [X] Backend dependency install failed. See the errors above.
pause
exit /b 1

:env_ready
REM ---- 2. backend\.env ------------------------------
if exist "%~dp0backend\.env" goto env_file_ready
copy "%~dp0backend\.env.example" "%~dp0backend\.env" >NUL
echo [!] Created backend\.env from the template. Add an API key there,
echo     or choose a local Ollama model on the Settings page.
:env_file_ready

REM ---- 3. Frontend dependencies ---------------------
if exist "%~dp0frontend\node_modules" goto frontend_ready
echo [Setup] Installing frontend dependencies (a few minutes on first run)...
pushd "%~dp0frontend"
call npm install
if errorlevel 1 goto npm_failed
popd
goto frontend_ready

:npm_failed
popd
echo [X] Frontend install failed. Install Node.js 18+ first.
pause
exit /b 1

:frontend_ready
echo.
REM Backend bind address. Default 127.0.0.1 = this machine only.
REM The backend runs AI-generated code and drives the desktop, and its API has
REM no authentication. Expose it to the network only if you understand that:
REM     set ATLAS_HOST=0.0.0.0
if not defined ATLAS_HOST set "ATLAS_HOST=127.0.0.1"

echo [1/2] Starting backend  (port 8014, host %ATLAS_HOST%)...
REM /k keeps the window open if uvicorn crashes, so the error stays readable.
REM PYTHONUTF8 is cleared: an inherited empty value makes Python exit at start.
start "Atlas_Backend" cmd /k "cd /d "%~dp0backend" && set "PYTHONUTF8=" && .venv\Scripts\uvicorn.exe main:app --host %ATLAS_HOST% --port 8014"

echo [2/2] Starting frontend (port 3012)...
start "Atlas_Frontend" cmd /k "cd /d "%~dp0frontend" && set "BACKEND_PORT=8014" && set "NEXT_PUBLIC_BACKEND_PORT=8014" && npx next dev --port 3012"

echo.
echo ==================================================
echo  Atlas is running
echo    UI      : http://localhost:3012
echo    Backend : http://localhost:8014
echo ==================================================
echo.
pause
