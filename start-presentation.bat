@echo off
rem Double-click to start both apps in PRESENTATION MODE: one-click login as any
rem user and a "Switch user" menu, with its own demo database.
rem Close the normal launcher windows first - both use ports 8000 and 8001.
set "KUBBAR_DEMO_MODE=1"
set "DATABASE_NAME=db_presentation.sqlite3"
call "%~dp0start.bat"
