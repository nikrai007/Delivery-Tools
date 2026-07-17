#!/usr/bin/env bash
# ====================================================================
#  Delivery Toolbox - macOS/Linux launcher (waitress WSGI server)
# ====================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v uv >/dev/null 2>&1; then
    echo "[setup] uv not found -- installing (https://astral.sh/uv)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "[setup] Syncing dependencies (uv.lock)..."
uv sync

if [ ! -f ".env" ]; then
    echo "[setup] No .env found. Copying .env.example -> .env"
    cp .env.example .env
    echo "[setup] Edit .env to set FLASK_SECRET_KEY and ADMIN_PASSWORD before exposing to users."
fi

PORT="$(grep -E '^PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r')"
HOST="$(grep -E '^HOST=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r')"
PORT="${PORT:-5000}"
HOST="${HOST:-0.0.0.0}"

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "[error] Port $PORT is already in use."
    if [ "$(uname)" = "Darwin" ] && [ "$PORT" = "5000" ]; then
        echo "        On macOS, port 5000 is claimed by the AirPlay Receiver by default."
        echo "        Fix: System Settings -> General -> AirDrop & Handoff -> turn off 'AirPlay Receiver'"
        echo "        ...or set a different PORT= in .env and re-run."
    else
        echo "        Stop whatever is using it, or set a different PORT= in .env and re-run."
    fi
    exit 1
fi

echo "[run] Starting Delivery Toolbox on ${HOST}:${PORT}..."
exec uv run python -m waitress --listen="${HOST}:${PORT}" app:app
