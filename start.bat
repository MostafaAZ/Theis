@echo off
rem Double-click to set up and start both Kubbar demo applications (Windows).
cd /d "%~dp0"
title Kubbar demo launcher

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || goto :oldpython

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || (
        echo Removing a virtual environment made with an older Python...
        rmdir /s /q .venv
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment...
    %PY% -m venv .venv || goto :fail
)
set "VPY=.venv\Scripts\python.exe"

echo Installing requirements (first run takes a minute)...
"%VPY%" -m pip install -q -r requirements.txt || goto :fail

set "NEWDB="
if not exist db.sqlite3 set "NEWDB=1"
echo Preparing the database...
"%VPY%" manage.py migrate -v 0 || goto :fail
if defined NEWDB (
    echo Loading demo data...
    "%VPY%" manage.py seed_demo || goto :fail
)

echo Starting the IMS on http://localhost:8000 and the platform on http://localhost:8001
start "Kubbar IMS (close to stop)" cmd /k ""%VPY%" manage.py runserver 8000"
start "Kubbar B2B platform (close to stop)" cmd /k ""%VPY%" manage.py runserver 8001 --settings=kubbar.settings.b2b"
timeout /t 5 /nobreak >nul
start "" http://localhost:8000
start "" http://localhost:8001
echo.
echo Both apps are running in their own windows. Close those windows to stop them.
echo Password for every demo account: Kubbar2026demo
pause
exit /b 0

:oldpython
echo This computer's Python is older than 3.11.
:nopython
echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during installation), then run this file again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Scroll up to see the error.
pause
exit /b 1
