#!/usr/bin/env bash
# Double-click (macOS) or run ./start.command (Linux) to set up and start
# both Kubbar demo applications. Press Ctrl+C in this window to stop them.
set -e
cd "$(dirname "$0")"

# Find Python 3.11 or newer (macOS ships an older python3 that is too old).
ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
PY=""
for c in python3.13 python3.12 python3.11 \
         /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && ok "$(command -v "$c")"; then PY=$(command -v "$c"); break; fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.11 or newer is needed, but this computer only has an older version."
  echo "Install it from https://www.python.org/downloads/ and run this file again."
  read -r -p "Press Enter to close." _
  exit 1
fi
echo "Using $("$PY" --version) at $PY"

# A virtual environment made earlier with an older Python must be rebuilt.
if [ -x .venv/bin/python ] && ! ok .venv/bin/python; then
  echo "Removing a virtual environment made with an older Python..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtual environment..."
  "$PY" -m venv .venv
fi
VPY=.venv/bin/python

echo "Installing requirements (first run takes a minute)..."
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q -r requirements.txt

DB="${DATABASE_NAME:-db.sqlite3}"
NEWDB=""
[ -f "$DB" ] || NEWDB=1
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
 if [ "${KUBBAR_DEMO_MODE:-}" = "1" ]; then urls="http://localhost:8000/login/ http://localhost:8001/login/";
 else urls="http://localhost:8000 http://localhost:8001"; fi
 for url in $urls; do
   if command -v open >/dev/null; then open "$url"; elif command -v xdg-open >/dev/null; then xdg-open "$url"; fi
 done) &

echo
[ "${KUBBAR_DEMO_MODE:-}" = "1" ] && echo "PRESENTATION MODE: one-click logins, separate demo database ($DB)."
echo "IMS: http://localhost:8000   Platform: http://localhost:8001"
echo "Password for every demo account: Kubbar2026demo"
echo "Press Ctrl+C to stop both apps."
"$VPY" manage.py runserver 8000
