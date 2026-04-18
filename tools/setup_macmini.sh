#!/usr/bin/env bash
# One-shot bootstrap for AegisData MVP on a fresh Mac mini.
#
#   bash tools/setup_macmini.sh
#
# Idempotent — safe to re-run. Exits with a clear message and instructions
# whenever a step needs human action (OrbStack first-launch permission,
# real API keys in .env, etc.).
#
# What it does (in order):
#   1.  ensure Homebrew is installed
#   2.  ensure OrbStack is installed (and running)
#   3.  ensure uv is installed
#   4.  create .env from .env.example if missing (warn about API keys)
#   5.  delete .venv if present (avoids OneDrive cross-machine pollution)
#       and run `uv sync` fresh
#   6.  uv run pytest -q          (verify host-side checks pass)
#   7.  docker compose build && up -d
#   8.  poll /healthz until 200
#   9.  bash tools/test_hook.sh   (10 hook smoke tests)
#  10.  print URLs + the install_hook.py command for the user to run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }

bold "AegisData MVP — Mac mini bootstrap"
echo "Project root: $PROJECT_ROOT"
echo

# ---------- 1. Homebrew ----------
if ! command -v brew >/dev/null 2>&1; then
    bold "[1/10] Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for this session (Apple Silicon path)
    if [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
else
    bold "[1/10] Homebrew already installed: $(brew --version | head -1)"
fi

# ---------- 2. OrbStack ----------
if [ ! -d /Applications/OrbStack.app ]; then
    bold "[2/10] Installing OrbStack…"
    brew install --cask orbstack
fi
export PATH="$HOME/.orbstack/bin:$PATH"

if ! command -v docker >/dev/null 2>&1; then
    yellow "[2/10] Docker CLI not in PATH yet."
    yellow "       Open OrbStack.app, complete the first-time setup (approve permissions),"
    yellow "       then re-run this script."
    open -a OrbStack >/dev/null 2>&1 || true
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    yellow "[2/10] OrbStack is installed but the daemon isn't reachable."
    yellow "       Open OrbStack.app and wait for the menu-bar icon to go solid, then re-run."
    open -a OrbStack >/dev/null 2>&1 || true
    exit 1
fi
bold "[2/10] OrbStack OK ($(docker --version))"

# ---------- 3. uv ----------
if ! command -v uv >/dev/null 2>&1; then
    bold "[3/10] Installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
bold "[3/10] uv OK ($(uv --version))"

# ---------- 4. .env ----------
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        yellow "[4/10] Created .env from .env.example."
        yellow "       Edit it now to put REAL API keys + switch providers from dummy → openai/haiku:"
        yellow "          ANTHROPIC_API_KEY=sk-ant-..."
        yellow "          OPENAI_API_KEY=sk-..."
        yellow "          AEGIS_EMBEDDING_PROVIDER=openai"
        yellow "          AEGIS_JUDGE_PROVIDER=haiku"
        yellow "       File: $PROJECT_ROOT/.env"
        yellow "       Then re-run this script."
        exit 1
    else
        red "[4/10] .env.example missing — incomplete project tree."
        exit 1
    fi
fi
bold "[4/10] .env exists"
inj_check() { awk -F= -v k="$1" '$1==k {print length($2)}' .env; }
if [ "$(inj_check ANTHROPIC_API_KEY)" -lt 30 ] 2>/dev/null; then
    yellow "       (warning) ANTHROPIC_API_KEY looks empty/placeholder."
fi

# ---------- 5. .venv (fresh) ----------
if [ -d .venv ]; then
    yellow "[5/10] Removing existing .venv (avoids OneDrive cross-machine pollution)…"
    rm -rf .venv
fi
bold "[5/10] uv sync…"
uv sync

# ---------- 6. pytest ----------
bold "[6/10] uv run pytest -q…"
uv run pytest -q

# ---------- 7. docker build + up ----------
bold "[7/10] docker compose build && up -d…"
docker compose build
docker compose up -d

# ---------- 8. /healthz ----------
bold "[8/10] Waiting for /healthz…"
for _ in $(seq 1 30); do
    if curl -sf http://localhost:8000/healthz >/dev/null 2>&1; then
        green "       service healthy"
        break
    fi
    sleep 1
done
if ! curl -sf http://localhost:8000/healthz >/dev/null 2>&1; then
    red "       /healthz still not responding after 30s — check 'docker compose logs'"
    exit 1
fi

# ---------- 9. hook smoke tests ----------
bold "[9/10] Hook smoke tests…"
bash "$SCRIPT_DIR/test_hook.sh"

# ---------- 10. summary ----------
bold "[10/10] Done."
echo
green "✓ Aegis is running at http://localhost:8000"
echo "    dashboard:    http://localhost:8000/"
echo "    theater:      http://localhost:8000/theater"
echo "    attestation:  http://localhost:8000/attestation"
echo "    OpenAPI docs: http://localhost:8000/docs"
echo
echo "Container will auto-restart on reboot (restart: unless-stopped)."
echo
bold "Next: install the Claude Code PreToolUse hook."
echo "Run:"
echo
echo "    python3 $SCRIPT_DIR/install_hook.py"
echo
echo "Then restart Claude Code."
