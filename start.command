#!/bin/zsh
set -e -o pipefail

APP_DIR="${0:A:h}"
RECORDINGS_DIR="${DAVIS_RECORDINGS_DIR:-/Users/yoosunghwan/davis_recordings}"
PYTHON_BIN="${DAVIS_PYTHON:-/Users/yoosunghwan/davis_event_recorder/.venv/bin/python}"
VIEWER_PORT="${DAVIS_VIEWER_PORT:-8765}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python environment not found: $PYTHON_BIN"
  exit 1
fi

SERVER_PID=""
if ! /usr/bin/curl -fsS "http://127.0.0.1:$VIEWER_PORT/health" >/dev/null 2>&1; then
  "$PYTHON_BIN" "$APP_DIR/server.py" --recordings "$RECORDINGS_DIR" --port "$VIEWER_PORT" &
  SERVER_PID=$!
fi
trap 'if [[ -n "$SERVER_PID" ]]; then kill "$SERVER_PID" 2>/dev/null || true; fi' EXIT INT TERM

for attempt in {1..40}; do
  if /usr/bin/curl -fsS "http://127.0.0.1:$VIEWER_PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! /usr/bin/curl -fsS "http://127.0.0.1:$VIEWER_PORT/health" >/dev/null; then
  echo "Viewer did not start. Check the error above."
  exit 1
fi

echo ""
echo "Local viewer: http://127.0.0.1:$VIEWER_PORT"
echo "Keep this window open while sharing. Press Control-C to stop."
echo ""

if command -v cloudflared >/dev/null 2>&1; then
  echo "Creating the internet link. Look for the trycloudflare.com URL below:"
  cloudflared tunnel --url "http://127.0.0.1:$VIEWER_PORT" --no-autoupdate 2>&1 | tee "$APP_DIR/data/tunnel.log"
else
  echo "cloudflared is not installed. Local viewing is available above."
fi
