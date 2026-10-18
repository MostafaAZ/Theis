#!/usr/bin/env bash
# Double-click (macOS) or run ./start-presentation.command (Linux) to start both
# apps in PRESENTATION MODE: one-click login as any user and a "Switch user"
# menu. It uses its own demo database, separate from the normal one.
# Stop the normal launcher first – both use ports 8000 and 8001.
export KUBBAR_DEMO_MODE=1
export DATABASE_NAME=db_presentation.sqlite3
exec bash "$(dirname "$0")/start.command"
