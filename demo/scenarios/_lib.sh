#!/usr/bin/env bash
# Shared helpers for demo/scenarios/*.sh scripts.
#
# Every scenario sources this and uses the functions below. Functions
# are idempotent — reboot the Docker container only if it's not up,
# tolerate repeated calls, etc.

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# Color + formatting helpers
# ─────────────────────────────────────────────────────────────────────
_C_BLUE=$'\033[1;34m'
_C_GREEN=$'\033[1;32m'
_C_YELLOW=$'\033[1;33m'
_C_RED=$'\033[1;31m'
_C_CYAN=$'\033[1;36m'
_C_DIM=$'\033[2m'
_C_BOLD=$'\033[1m'
_C_RESET=$'\033[0m'

scenario() {
  local title="$1"
  echo
  echo "${_C_BLUE}╔══════════════════════════════════════════════════════════════════════╗${_C_RESET}"
  printf "${_C_BLUE}║${_C_BOLD}  %-68s${_C_RESET}${_C_BLUE}║${_C_RESET}\n" "$title"
  echo "${_C_BLUE}╚══════════════════════════════════════════════════════════════════════╝${_C_RESET}"
}

stage() {
  echo
  echo "${_C_CYAN}  ▸ $1${_C_RESET}"
}

note() { echo "${_C_DIM}    # $*${_C_RESET}"; }
pass() { echo "${_C_GREEN}    ✓ $*${_C_RESET}"; }
fail() { echo "${_C_RED}    ✗ $*${_C_RESET}"; SCENARIO_FAILED=1; }
info() { echo "${_C_YELLOW}    → $*${_C_RESET}"; }

run() {
  echo "${_C_DIM}    $ $*${_C_RESET}"
  "$@"
}

# ─────────────────────────────────────────────────────────────────────
# Service lifecycle
# ─────────────────────────────────────────────────────────────────────
AEGIS_URL="${AEGIS_URL:-http://localhost:8000}"

ensure_aegis() {
  if curl -sf "$AEGIS_URL/healthz" >/dev/null 2>&1; then
    return 0
  fi
  note "Aegis service not reachable at $AEGIS_URL — bringing up Docker"
  (cd "$(git rev-parse --show-toplevel)" && docker compose up -d >/dev/null 2>&1 || true)
  local waited=0
  until curl -sf "$AEGIS_URL/healthz" >/dev/null 2>&1; do
    sleep 1
    waited=$((waited + 1))
    if [[ $waited -gt 30 ]]; then
      fail "service did not come up in 30s at $AEGIS_URL"
      return 1
    fi
  done
  note "service up — burn_in_id = $(curl -s "$AEGIS_URL/healthz" | python3 -c "import json,sys;print(json.load(sys.stdin).get('burn_in_id',''))[:16]")"
}

# ─────────────────────────────────────────────────────────────────────
# Assertion helpers
# ─────────────────────────────────────────────────────────────────────
SCENARIO_FAILED=0

assert_eq() {
  local expected="$1" actual="$2" msg="${3:-}"
  if [[ "$expected" == "$actual" ]]; then
    pass "$msg  (got: $actual)"
  else
    fail "$msg — expected: $expected  got: $actual"
  fi
}

assert_contains() {
  local needle="$1" haystack="$2" msg="${3:-}"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass "$msg"
  else
    fail "$msg — '$needle' not found in output"
    echo "${_C_DIM}         output: ${haystack:0:200}…${_C_RESET}"
  fi
}

assert_gte() {
  local threshold="$1" actual="$2" msg="${3:-}"
  if [[ "$actual" -ge "$threshold" ]]; then
    pass "$msg  (got: $actual ≥ $threshold)"
  else
    fail "$msg — expected ≥ $threshold, got $actual"
  fi
}

# ─────────────────────────────────────────────────────────────────────
# Request builders
# ─────────────────────────────────────────────────────────────────────
ns() { python3 -c "import time; print(time.time_ns())"; }
uuid() { python3 -c "import uuid; print(uuid.uuid4())"; }

# Build an ATVInput payload. Delegates to _payload.py to avoid bash
# heredoc quoting issues with arbitrary JSON.
#   build_payload <tool> <args_json> <aid> [extra _payload.py args...]
build_payload() {
  python3 "$(dirname "${BASH_SOURCE[0]}")/_payload.py" "$@"
}

evaluate() {
  # Usage: evaluate <payload_json>
  curl -s -X POST "$AEGIS_URL/evaluate" \
    -H 'content-type: application/json' \
    -d "$1"
}

# ─────────────────────────────────────────────────────────────────────
# Finalizer — each scenario ends with this
# ─────────────────────────────────────────────────────────────────────
scenario_end() {
  echo
  if [[ $SCENARIO_FAILED -eq 0 ]]; then
    echo "${_C_GREEN}  ╭──────────────────────╮${_C_RESET}"
    echo "${_C_GREEN}  │  SCENARIO: ✓ PASS    │${_C_RESET}"
    echo "${_C_GREEN}  ╰──────────────────────╯${_C_RESET}"
    exit 0
  else
    echo "${_C_RED}  ╭──────────────────────╮${_C_RESET}"
    echo "${_C_RED}  │  SCENARIO: ✗ FAIL    │${_C_RESET}"
    echo "${_C_RED}  ╰──────────────────────╯${_C_RESET}"
    exit 1
  fi
}
