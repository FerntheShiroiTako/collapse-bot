@echo off
rem Collapse launcher for Windows.
rem   run.bat          start the bot (creates .venv and installs dependencies on first run)
rem   run.bat update   reinstall dependencies, e.g. after pulling new code
rem   run.bat genkey   print a new MASTER_KEY for .env
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :nopython
    call :install || goto :fail
)

".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 12))"
if errorlevel 1 (
    echo This virtual environment's Python is older than 3.12. Delete the .venv folder and install Python 3.12+.
    goto :fail
)

if /i "%~1"=="update" (
    call :install || goto :fail
    echo Dependencies updated.
    goto :eof
)
if /i "%~1"=="genkey" (
    ".venv\Scripts\python.exe" -m banbot genkey
    goto :eof
)

if not exist ".env" (
    echo No .env file found. Copy .env.example to .env and fill it in first:
    echo     copy .env.example .env
    goto :fail
)

echo Starting Collapse. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m banbot
set EXITCODE=%ERRORLEVEL%
echo.
echo Bot stopped ^(exit code %EXITCODE%^).
pause
exit /b %EXITCODE%

:install
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet || exit /b 1
".venv\Scripts\python.exe" -m pip install -e . --quiet || exit /b 1
exit /b 0

:nopython
echo Could not create a virtual environment. Install Python 3.12 or newer from python.org first.
:fail
pause
exit /b 1
