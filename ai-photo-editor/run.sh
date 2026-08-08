#!/usr/bin/env bash
# Start the editor.
#
#   ./run.sh          -> http://127.0.0.1:8000   (this machine only)
#   ./run.sh --lan    -> also reachable from your phone on the same Wi-Fi
set -euo pipefail

cd "$(dirname "$0")"

HOST=127.0.0.1
if [ "${1:-}" = "--lan" ]; then
  HOST=0.0.0.0
fi
PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if [ "$HOST" = "0.0.0.0" ]; then
  # Best-effort LAN address so you can type it into a phone.
  IP=$(ipconfig getifaddr en0 2>/dev/null \
    || ipconfig getifaddr en1 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}' \
    || echo "")
  echo
  echo "  On this machine : http://127.0.0.1:${PORT}"
  [ -n "$IP" ] && echo "  On your phone   : http://${IP}:${PORT}   (same Wi-Fi)"
  echo
  echo "  This serves on your local network. Don't run it on Wi-Fi you don't trust —"
  echo "  anyone on the network can open it and use your API key."
  echo
fi

exec ./.venv/bin/python -m uvicorn server.app:app --host "$HOST" --port "$PORT" --reload
