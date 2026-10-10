#!/usr/bin/env bash
# Double-click (macOS) or run ./start.command (Linux) to set up and start
# both Kubbar demo applications. Press Ctrl+C in this window to stop them.
set -e
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is required: https://www.python.org/downloads/"
  read -r -p "Press Enter to close." _
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtual environment..."
  "$PY" -m venv .venv
fi
VPY=.venv/bin/python

echo "Installing requirements (first run takes a minute)..."
"$VPY" -m pip install -q -r requirements.txt

NEWDB=""
[ -f db.sqlite3 ] || NEWDB=1
echo "Preparing the database..."
"$VPY" manage.py migrate -v 0
if [ -n "$NEWDB" ]; then
  echo "Loading demo data..."
  "$VPY" manage.py seed_demo
fi

"$VPY" manage.py runserver 8001 --settings=kubbar.settings.b2b &
B2B_PID=$!
trap 'kill $B2B_PID 2>/dev/null' EXIT

(sleep 4
 for url in http://localhost:8000 http://localhost:8001; do
   if command -v open >/dev/null; then open "$url"; elif command -v xdg-open >/dev/null; then xdg-open "$url"; fi
 done) &

echo
echo "IMS: http://localhost:8000   Platform: http://localhost:8001"
echo "Password for every demo account: Kubbar2026demo"
echo "Press Ctrl+C to stop both apps."
"$VPY" manage.py runserver 8000
