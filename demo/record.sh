#!/usr/bin/env bash
# AegisData T2 — recordable demo script.
#
# Drives the full M8-M16 surface in one take. Designed to be captured
# with asciinema:
#
#     asciinema rec demo/recording/demo.cast \
#       --title "AegisData T2 demo" \
#       --idle-time-limit 1.5 \
#       --command "bash demo/record.sh"
#
# Then turn into a GIF:
#
#     agg --theme monokai demo/recording/demo.cast demo/recording/demo.gif
#
# Or just run it standalone to produce a plain transcript:
#
#     bash demo/record.sh | tee demo/recording/transcript.log
#
# Total runtime: ~30 seconds against a warm Docker image.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ─────────────────────────────────────────────────────────────────────
# pretty helpers
# ─────────────────────────────────────────────────────────────────────
_C_BLUE=$'\033[1;34m'
_C_GREEN=$'\033[1;32m'
_C_YELLOW=$'\033[1;33m'
_C_RED=$'\033[1;31m'
_C_DIM=$'\033[2m'
_C_RESET=$'\033[0m'

scene() {
  local title="$1"
  echo
  echo "${_C_BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${_C_RESET}"
  echo "${_C_BLUE}  ${title}${_C_RESET}"
  echo "${_C_BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${_C_RESET}"
}

note() { echo "${_C_DIM}# $*${_C_RESET}"; }
ok()   { echo "${_C_GREEN}✓ $*${_C_RESET}"; }
run()  { echo "${_C_YELLOW}\$ $*${_C_RESET}"; sleep 0.3; eval "$@"; }

PORT="${AEGIS_PORT:-8000}"
URL="http://localhost:${PORT}"
export AEGIS_URL="$URL"

# ─────────────────────────────────────────────────────────────────────
# 0. Title card
# ─────────────────────────────────────────────────────────────────────
clear
cat <<'BANNER'
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║          AegisData T2 — Action Firewall for AI Agents             ║
║                                                                   ║
║   2080-D ATV  ·  Ed25519 + Merkle audit  ·  AES-256-GCM journal   ║
║   ATMU 2-phase commit  ·  AID circuit breaker  ·  HAM L3+L4       ║
║                                                                   ║
║              16 patent-aligned milestones · 326 tests             ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
BANNER
sleep 2

# ─────────────────────────────────────────────────────────────────────
# 1. Boot
# ─────────────────────────────────────────────────────────────────────
scene "1/6  ·  Boot the service (one container)"

note "If the container is already running, this is a no-op."
run "docker compose up -d 2>&1 | tail -3"

note "Wait for /healthz green."
until curl -sf "$URL/healthz" >/dev/null 2>&1; do sleep 0.5; done
run "curl -s $URL/healthz | jq"

# ─────────────────────────────────────────────────────────────────────
# 2. Single firewall verdict — show the full step trace
# ─────────────────────────────────────────────────────────────────────
scene "2/6  ·  POST /evaluate — one tool call, full firewall trace"

note "Build an ATV-2080-v1 input and ask Aegis what to do."
PAYLOAD='{
  "header": {
    "trace_id": "demo-001", "span_id": "span-001",
    "tenant_id": "demo-tenant", "aid": "demo-recording-agent",
    "ats": "ATV-2080-v1", "schema_version": "ATV-2080-v1",
    "tier_profile": "T2", "cost_attestation_profile": "software",
    "timestamp_ns": 1737172800000000000
  },
  "agent_state_text": "user asked to read the Q3 report",
  "plan_text": "read ./data/report.txt",
  "tool_name": "read_file",
  "tool_args_json": "{\"path\":\"./data/report.txt\"}",
  "safety_flags": {"prompt_injection": 0.02},
  "cost_estimate": {
    "input_token_count": 120,
    "cumulative_dollars": 0.0001,
    "forecasted_cost_to_completion": 0.01
  }
}'

run "curl -s $URL/evaluate -H content-type:application/json -d '\$PAYLOAD' | jq '{decision, reason, atv_id, signature: (.signature[:24] + \"…\"), step_traces}'"

# ─────────────────────────────────────────────────────────────────────
# 3. Audit chain — Ed25519 + Merkle
# ─────────────────────────────────────────────────────────────────────
scene "3/6  ·  GET /audit/{aid} — the signed Merkle chain"

note "Every record signed Ed25519, prev_hash → this_hash linked."
run "curl -s $URL/audit/demo-recording-agent | jq '{length, head: (.head[:24] + \"…\"), chain_valid}'"

# ─────────────────────────────────────────────────────────────────────
# 4. Run the canned demo (5 calls + M14/M15/M16 extensions)
# ─────────────────────────────────────────────────────────────────────
scene "4/6  ·  Full demo — 5-call firewall + M14 + M16"

note "5 hand-crafted calls (ALLOW/BLOCK/APPROVAL mix) + circuit-breaker"
note "scenario + Hierarchical Agent Memory exercise."

# Use .venv if present, else uv.
if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  PY="uv run python"
fi
run "$PY -m demo.agent_demo"

# ─────────────────────────────────────────────────────────────────────
# 5. Forensic replay — AES-256-GCM journal walk
# ─────────────────────────────────────────────────────────────────────
scene "5/6  ·  GET /forensic/replay — decrypt every audit record"

note "Walks the AES-256-GCM journal end-to-end, rebuilds per-AID chains."
run "curl -s $URL/forensic/replay | jq '{available, decrypted_count, tampered_count, aids_seen, chains_valid: (.per_aid_chain_valid | to_entries | map(.value) | all)}'"

# ─────────────────────────────────────────────────────────────────────
# 6. Cost attestation + AID admin (M12 + M14 surface check)
# ─────────────────────────────────────────────────────────────────────
scene "6/6  ·  Surface check — /admin/aid + /cost-attestation"

note "Per-AID quarantine status (M14)."
run "curl -s $URL/admin/aid | jq '{quarantined: (.quarantined | length)}'"

note "Per-tenant cost ledger (M12, separate Ed25519 key per Claim 34)."
run "curl -s $URL/cost-attestation/by-tenant/demo-tenant | jq '{tenant_id, length, sample_model: .records[0].model_name, sample_atv_commitment: (.records[0].atv_commitment[:24] + \"…\")}'"

# ─────────────────────────────────────────────────────────────────────
# Closing card
# ─────────────────────────────────────────────────────────────────────
echo
cat <<'CLOSING'
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║   Demo complete.                                                  ║
║                                                                   ║
║   Live dashboard:    http://localhost:8000                        ║
║   ATV Theater:       http://localhost:8000/theater                ║
║   API docs:          http://localhost:8000/docs                   ║
║                                                                   ║
║   Next:                                                           ║
║     docs/QUICKSTART.md     — 60-second install                    ║
║     docs/ARCHITECTURE.md   — per-milestone surface tour           ║
║     docs/OPERATIONS.md     — production runbook                   ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
CLOSING
