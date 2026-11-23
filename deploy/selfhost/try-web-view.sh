#!/usr/bin/env bash
# Try dayfile's read-only web view with sample events, then clean up. Nothing is installed outside a
# temporary directory and nothing is left running. Needs Python 3.11 or newer.
#
#   bash try-web-view.sh                       # installs "dayfile[web]" from PyPI
#   FROM=/path/to/checkout bash try-web-view.sh   # or any pip target, such as a git URL
set -euo pipefail
FROM="${FROM:-dayfile[web]}"
API="${API_PORT:-8000}" WEB="${WEB_PORT:-8001}"
case "$FROM" in *"["*) ;; *) FROM="$FROM[web]" ;; esac   # a path or URL needs the extra too

T="$(mktemp -d)"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
  rm -rf "$T"
  echo "cleaned up"
}
trap cleanup EXIT INT TERM

echo "installing $FROM into a temporary virtualenv..."
python3 -m venv "$T/venv"
"$T/venv/bin/pip" install -q --disable-pip-version-check "$FROM"
export PATH="$T/venv/bin:$PATH" DAYFILE_DB="$T/day.db"

READ_URL="$(dayfile token create --labels self | sed -n 2p)"

uvicorn dayfile.app:app --port "$API" --log-level warning & PIDS+=($!)
DAYFILE_SINGLE_LOG=1 DAYFILE_API_URL="http://127.0.0.1:$API" \
  uvicorn dayfile.web:app --port "$WEB" --log-level warning & PIDS+=($!)
for _ in $(seq 40); do
  curl -sf "localhost:$API/healthz" >/dev/null && curl -sf "localhost:$WEB/healthz" >/dev/null && break
  sleep 0.5
done

dayfile add --db "$DAYFILE_DB" "hello from the web view" >/dev/null
dayfile add --db "$DAYFILE_DB" --todo "check the web view" >/dev/null

echo
echo "Open http://localhost:$WEB and paste this into the sign-in box:"
echo
echo "  $READ_URL"
echo
echo "A new event is added every 15 seconds so you can watch the page update. Ctrl-C to stop and clean up."
n=0
while sleep 15; do n=$((n + 1)); dayfile add --db "$DAYFILE_DB" "live update $n" >/dev/null; done
