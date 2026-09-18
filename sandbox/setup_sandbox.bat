@echo off
REM Switch to UTF-8 codepage so the rest of this file (containing CJK chars
REM in echo messages and prompts) is parsed correctly by CMD. Without this,
REM Traditional Chinese Windows runs CMD in CP950/Big5, which mis-parses the
REM UTF-8 multibyte sequences and CMD treats the tail of a misparsed line as
REM a separate command -- causing errors like "'LF' is not recognized".
chcp 65001 >NUL

REM Atlas - Sandbox one-click installer (Windows entry)
REM
REM What this does:
REM   1. Verifies WSL is installed (otherwise tells user to run wsl --install)
REM   2. Translates the project path to WSL form (/mnt/c/...)
REM   3. Calls sandbox/setup.sh inside WSL to do the actual install
REM
REM Usage:
REM   setup_sandbox.bat              - normal install (first clone)
REM   setup_sandbox.bat --rebuild    - force rebuild image
REM                                    (use after editing Dockerfile / requirements.txt)
SETLOCAL
SET "EXTRA_ARGS=%*"

echo ==================================================
echo  Atlas - Sandbox Setup
echo ==================================================
echo.

REM 1. Verify WSL is installed
wsl --status >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [X] WSL not detected.
    echo.
    echo Please run the following in an Administrator PowerShell:
    echo.
    echo     wsl --install
    echo.
    echo Then reboot, and run this script again.
    echo.
    pause
    exit /b 1
)

REM 2. Verify a usable WSL distro exists (Ubuntu etc.)
wsl -e bash -c "echo OK" >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [X] No usable WSL distro.
    echo Please install Ubuntu via: wsl --install -d Ubuntu
    pause
    exit /b 1
)

REM 3. Translate project dir to WSL path form
SET "WIN_PROJECT=%~dp0.."
FOR /F "usebackq tokens=*" %%F IN (`wsl wslpath -a "%WIN_PROJECT%"`) DO SET "WSL_PROJECT=%%F"

echo Windows project : %WIN_PROJECT%
echo WSL project     : %WSL_PROJECT%
echo.

REM 4. Self-heal CRLF: Windows git's default core.autocrlf=true rewrites .sh
REM    files to CRLF on checkout, which breaks WSL bash (set -euo pipefail
REM    becomes "set -euo pipefail\r" -> invalid option). Strip the \r from
REM    setup.sh before invoking it. Idempotent for already-LF files.
wsl -e bash -c "sed -i 's/\r$//' '%WSL_PROJECT%/sandbox/setup.sh'" >NUL 2>&1

REM 5. Run the WSL-side setup script
echo === Running setup inside WSL ===
echo (If this is the first time, you may be prompted for your WSL password for sudo)
echo.
wsl bash "%WSL_PROJECT%/sandbox/setup.sh" "%WSL_PROJECT%" %EXTRA_ARGS%

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo !! Setup FAILED. See messages above for details.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==================================================
echo  All done. Sandbox is ready.
echo ==================================================
echo.

REM Detect: docker freshly installed, docker group not yet reloaded
REM -> we need wsl --shutdown
IF EXIST "%~dp0.needs_wsl_shutdown" (
    echo ==================================================
    echo  [!] IMPORTANT: WSL needs a restart before starting Atlas
    echo ==================================================
    echo.
    echo  Docker was just installed and your user was added to the docker
    echo  group, but the current WSL session is still on old permissions.
    echo  If you start the backend without restarting WSL, every skill run
    echo  will be blocked on a sudo password prompt.
    echo.
    SET /P SHUTDOWN_ANS="Run wsl --shutdown now? (Y/N, default Y): "
    IF /I NOT "%SHUTDOWN_ANS%"=="N" (
        echo.
        echo ==^> wsl --shutdown ...
        wsl --shutdown
        echo (V) WSL stopped. It will auto-restart with docker group permissions next time.
    ) ELSE (
        echo.
        echo [i] Remember to run manually: wsl --shutdown
    )
    del "%~dp0.needs_wsl_shutdown" >NUL 2>&1
    echo.
)

echo Next: start Atlas normally (launch.bat or
echo uvicorn + npm dev). The backend will auto-detect
echo the sandbox and route skill code through it when the
echo "Sandbox execution" toggle is on in Settings.
echo.
pause
