#!/usr/bin/env bash
# Convenience: bring the Aegis service up locally and run the demo.
# Picks docker-compose if Docker is available, otherwise uvicorn in the
# foreground of a background subshell.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

cleanup() {
    if [[ -n "${SERVER_PID:-}" ]]; then
        echo "[run_scenario] stopping local server (pid=$SERVER_PID)"
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    if [[ "${USED_DOCKER:-0}" == "1" ]]; then
        echo "[run_scenario] stopping docker compose"
        docker compose down >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "[run_scenario] starting via docker compose"
    docker compose up --build -d
    USED_DOCKER=1
else
    echo "[run_scenario] starting via uv run uvicorn (no Docker)"
    uv run uvicorn aegis.main:app --host 127.0.0.1 --port 8000 \
        > /tmp/aegis-demo.log 2>&1 &
    SERVER_PID=$!
fi

echo "[run_scenario] waiting for /healthz ..."
for _ in $(seq 1 30); do
    if curl -sf http://localhost:8000/healthz >/dev/null; then
        echo "[run_scenario] service is up."
        break
    fi
    sleep 0.5
done

uv run python -m demo.agent_demo
