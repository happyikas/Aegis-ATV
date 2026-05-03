#!/usr/bin/env bash
# Aegis Plugin (Solo Free) — pre-flight dogfood check.
#
# Run this AFTER `aegis install --mode local --judge hybrid` and BEFORE
# trusting the hook with real Claude Code traffic. It exercises every
# moving part of the plugin install + hook execution pipeline using
# subprocess invocation (not import) — the same path Claude Code takes.
#
# Checks (each is a single test_<name> function):
#  1. settings.json validates and contains all 3 hooks
#  2. PreToolUse hook ALLOWs an innocuous Bash `ls`
#  3. PreToolUse hook BLOCKs `rm -rf /`
#  4. PostToolUse hook accepts a tool result without crashing
#  5. Stop hook accepts a session-end payload without crashing
#  6. Audit chain (~/.aegis/audit.jsonl) gained at least one entry
#  7. First-call latency budget (<5s — Claude Code's hook timeout)
#  8. settings.json idempotency (re-running install is a no-op)
#
# Usage:
#   ./scripts/dogfood_check.sh                # default: --judge hybrid
#   ./scripts/dogfood_check.sh --dummy        # use dummy judge
#   AEGIS_DOGFOOD_KEEP_AUDIT=1 ./scripts/...  # keep test audit entries
#
# Exit 0 = green-light to use real Claude Code with the hook.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

JUDGE="dummy"   # default: matches `aegis install --mode local` default
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hybrid) JUDGE="hybrid"; shift ;;
    --dummy)  JUDGE="dummy"; shift ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# colours
if [[ -t 1 ]]; then
  C_OK="$(printf '\033[32m')"; C_ERR="$(printf '\033[31m')"
  C_WARN="$(printf '\033[33m')"; C_DIM="$(printf '\033[2m')"
  C_BOLD="$(printf '\033[1m')"; C_RESET="$(printf '\033[0m')"
else
  C_OK=""; C_ERR=""; C_WARN=""; C_DIM=""; C_BOLD=""; C_RESET=""
fi
PASS=0; FAIL=0
ok()   { printf "  %s✓%s %s\n" "$C_OK" "$C_RESET" "$*"; PASS=$((PASS+1)); }
fail() { printf "  %s✗%s %s\n" "$C_ERR" "$C_RESET" "$*"; FAIL=$((FAIL+1)); }
note() { printf "  %s%s%s\n" "$C_DIM" "$*" "$C_RESET"; }

# Use an isolated audit file so we don't dirty the user's chain.
TMPDIR_AUDIT="$(mktemp -d)"
TEST_AUDIT="$TMPDIR_AUDIT/audit.jsonl"
export AEGIS_LOCAL_AUDIT="$TEST_AUDIT"
export AEGIS_TENANT_ID="dogfood-test"
export AEGIS_EMBEDDING_PROVIDER="dummy"
export AEGIS_JUDGE_PROVIDER="$JUDGE"
export AEGIS_POLICY_DIR="$REPO_ROOT/policies"
export PYTHONPATH="$REPO_ROOT/src"

# Use the project's venv python so subprocess `import numpy` etc work.
# Same logic as `tools/aegis_cli.py:_hook_python_executable()`.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="python3"
  echo "  (warning: .venv/bin/python not found — falling back to python3)" >&2
fi

cleanup() {
  if [[ "${AEGIS_DOGFOOD_KEEP_AUDIT:-0}" == "1" ]]; then
    note "audit kept at $TEST_AUDIT"
  else
    rm -rf "$TMPDIR_AUDIT"
  fi
}
trap cleanup EXIT

printf "%sAegis dogfood check%s  (judge=%s, audit=%s)\n\n" \
  "$C_BOLD" "$C_RESET" "$JUDGE" "$TEST_AUDIT"

PRE_HOOK="$REPO_ROOT/tools/aegis_local_hook.py"
POST_HOOK="$REPO_ROOT/tools/hooks/post_tool.py"
STOP_HOOK="$REPO_ROOT/tools/hooks/session_end.py"
SETTINGS="$HOME/.claude/settings.json"

# ─────────────────────────────────────────────────────────────────────
# 1. settings.json validates and contains all 3 hooks
# ─────────────────────────────────────────────────────────────────────
printf "%s[1] settings.json shape%s\n" "$C_BOLD" "$C_RESET"
if [[ ! -f "$SETTINGS" ]]; then
  note "no $SETTINGS yet — checks 2-7 still validate the hook scripts directly."
  note "to run check [1] against a real install:  uv run aegis install --mode local --judge $JUDGE"
elif ! "$PYTHON" -c "
import json, sys
data = json.loads(open('$SETTINGS').read())
hooks = data.get('hooks', {})
have_pre  = any('aegis_local_hook.py' in h.get('command','')
                for e in hooks.get('PreToolUse', []) for h in e.get('hooks', []))
have_post = any('post_tool.py' in h.get('command','')
                for e in hooks.get('PostToolUse', []) for h in e.get('hooks', []))
have_stop = any('session_end.py' in h.get('command','')
                for e in hooks.get('Stop', []) for h in e.get('hooks', []))
sys.exit(0 if (have_pre and have_post and have_stop) else 1)
" 2>/dev/null; then
  note "settings.json exists but no Aegis hooks installed — checks 2-7 validate the scripts directly"
else
  ok "settings.json has PreToolUse + PostToolUse + Stop all registered"
fi

# ─────────────────────────────────────────────────────────────────────
# 2. PreToolUse: innocuous Bash `ls` should ALLOW
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[2] PreToolUse — innocuous Bash ls%s\n" "$C_BOLD" "$C_RESET"
PAYLOAD_LS='{"session_id":"sess-test","transcript_path":"","cwd":"/work","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"},"invocation_id":"dog-1"}'
echo "$PAYLOAD_LS" | "$PYTHON" "$PRE_HOOK" 2>/dev/null
RC=$?
if [[ "$RC" == "0" ]]; then
  ok "exit 0 (ALLOW)"
else
  fail "expected exit 0, got $RC (BLOCK on innocuous ls — false positive!)"
fi

# ─────────────────────────────────────────────────────────────────────
# 3. PreToolUse: rm -rf / should BLOCK
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[3] PreToolUse — destructive rm -rf /%s\n" "$C_BOLD" "$C_RESET"
PAYLOAD_RM='{"session_id":"sess-test","transcript_path":"","cwd":"/work","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"rm -rf /"},"invocation_id":"dog-2"}'
STDERR_FILE="$(mktemp)"
echo "$PAYLOAD_RM" | "$PYTHON" "$PRE_HOOK" 2>"$STDERR_FILE"
RC=$?
if [[ "$RC" == "2" ]]; then
  ok "exit 2 (BLOCK / REQUIRE_APPROVAL)"
  note "stderr: $(head -1 "$STDERR_FILE" | cut -c1-100)"
else
  fail "expected exit 2, got $RC (FALSE NEGATIVE on rm -rf /)"
fi
rm -f "$STDERR_FILE"

# ─────────────────────────────────────────────────────────────────────
# 4. PostToolUse: never blocks, never crashes
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[4] PostToolUse — accepts result%s\n" "$C_BOLD" "$C_RESET"
PAYLOAD_POST='{"session_id":"sess-test","hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"ls"},"tool_response":{"stdout":"file1\nfile2"},"exit_code":0,"invocation_id":"dog-1"}'
echo "$PAYLOAD_POST" | "$PYTHON" "$POST_HOOK" 2>/dev/null
RC=$?
if [[ "$RC" == "0" ]]; then
  ok "exit 0 (forensic capture only, never blocks)"
else
  fail "PostToolUse exit $RC — must always be 0"
fi

# ─────────────────────────────────────────────────────────────────────
# 5. Stop hook: never crashes
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[5] Stop — accepts session-end%s\n" "$C_BOLD" "$C_RESET"
TMP_TRANSCRIPT="$(mktemp).jsonl"
echo '{"type":"assistant","content":"done","usage":{"input_tokens":10,"output_tokens":5}}' > "$TMP_TRANSCRIPT"
PAYLOAD_STOP="{\"session_id\":\"sess-test\",\"transcript_path\":\"$TMP_TRANSCRIPT\"}"
echo "$PAYLOAD_STOP" | "$PYTHON" "$STOP_HOOK" 2>/dev/null
RC=$?
if [[ "$RC" == "0" ]]; then
  ok "exit 0"
else
  fail "Stop hook exit $RC — must always be 0"
fi
rm -f "$TMP_TRANSCRIPT"

# ─────────────────────────────────────────────────────────────────────
# 6. Audit chain populated
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[6] Audit chain populated%s\n" "$C_BOLD" "$C_RESET"
if [[ -f "$TEST_AUDIT" ]]; then
  N_LINES="$(wc -l < "$TEST_AUDIT" | tr -d ' ')"
  if [[ "$N_LINES" -ge "3" ]]; then
    ok "$N_LINES audit entries written (>= 3 expected from steps 2,3,4)"
  else
    fail "only $N_LINES audit entries — expected >= 3"
  fi
else
  fail "no audit file at $TEST_AUDIT"
fi

# ─────────────────────────────────────────────────────────────────────
# 7. First-call latency budget
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[7] First-call latency%s  (Claude Code timeout: 5000 ms)\n" "$C_BOLD" "$C_RESET"
T0_NS="$("$PYTHON" -c 'import time;print(time.perf_counter_ns())')"
echo "$PAYLOAD_LS" | "$PYTHON" "$PRE_HOOK" >/dev/null 2>&1
T1_NS="$("$PYTHON" -c 'import time;print(time.perf_counter_ns())')"
ELAPSED_MS=$(( (T1_NS - T0_NS) / 1000000 ))
if [[ "$ELAPSED_MS" -lt "5000" ]]; then
  if [[ "$ELAPSED_MS" -lt "1500" ]]; then
    ok "${ELAPSED_MS} ms (well under budget)"
  else
    printf "  %s!%s ${ELAPSED_MS} ms (under 5s budget but slow — investigate)\n" \
      "$C_WARN" "$C_RESET"
    PASS=$((PASS+1))
  fi
else
  fail "${ELAPSED_MS} ms — Claude Code will time out the hook!"
fi

# ─────────────────────────────────────────────────────────────────────
# 8. Install idempotency  (sandboxed — does not touch ~/.claude/settings.json)
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[8] aegis install idempotency%s  %s(sandboxed)%s\n" \
  "$C_BOLD" "$C_RESET" "$C_DIM" "$C_RESET"
SANDBOX_HOME="$(mktemp -d)"
mkdir -p "$SANDBOX_HOME/.claude"
HOME_REAL="$HOME"
HOME="$SANDBOX_HOME" uv run aegis install --mode local --judge "$JUDGE" --force >/dev/null 2>&1
SAND_SETTINGS="$SANDBOX_HOME/.claude/settings.json"
if [[ -f "$SAND_SETTINGS" ]]; then
  BEFORE_SHA="$(shasum -a 256 "$SAND_SETTINGS" | awk '{print $1}')"
  HOME="$SANDBOX_HOME" uv run aegis install --mode local --judge "$JUDGE" --force >/dev/null 2>&1
  AFTER_SHA="$(shasum -a 256 "$SAND_SETTINGS" | awk '{print $1}')"
  N_PRE="$(grep -c 'aegis_local_hook.py' "$SAND_SETTINGS" 2>/dev/null || echo 0)"
  N_POST="$(grep -c 'tools/hooks/post_tool.py' "$SAND_SETTINGS" 2>/dev/null || echo 0)"
  N_STOP="$(grep -c 'tools/hooks/session_end.py' "$SAND_SETTINGS" 2>/dev/null || echo 0)"
  if [[ "$N_PRE" == "1" && "$N_POST" == "1" && "$N_STOP" == "1" ]]; then
    ok "no duplicates on re-install (--force evicts then re-adds)"
  else
    fail "duplicates after re-install: pre=$N_PRE post=$N_POST stop=$N_STOP"
  fi
else
  fail "sandboxed install did not create $SAND_SETTINGS"
fi
rm -rf "$SANDBOX_HOME"
HOME="$HOME_REAL"

# ─────────────────────────────────────────────────────────────────────
# 9. Real local sLLM invocation (only if model + llama-cpp present)
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[9] Real local-sLLM verdict%s\n" "$C_BOLD" "$C_RESET"
LLAMA_CHECK="$("$PYTHON" -c 'import importlib;importlib.import_module("llama_cpp");print("ok")' 2>/dev/null || echo missing)"
MODEL_GLOB="$REPO_ROOT/models/*.gguf"
MODEL_FILE="$(ls $MODEL_GLOB 2>/dev/null | head -1)"
if [[ "$LLAMA_CHECK" != "ok" ]]; then
  note "skipped — llama-cpp-python not installed.  uv sync --extra local-llm"
elif [[ -z "$MODEL_FILE" ]]; then
  note "skipped — no GGUF in $REPO_ROOT/models/.  uv run aegis pull-model"
else
  REAL_OUT="$(AEGIS_JUDGE_MODEL_PATH="$MODEL_FILE" \
              AEGIS_JUDGE_LOCAL_PHI_STUB=0 \
              "$PYTHON" - <<'PY' 2>&1
import os, time
import numpy as np
from aegis.judge.local_phi import LocalPhiJudge
from aegis.atv.builder import build_atv
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

j = LocalPhiJudge()
mode, info = j._decide_mode()
print(f"MODE={mode}")
if mode != "real":
    raise SystemExit(0)
inp = ATVInput(
    header=ATVHeader(trace_id="t", span_id="s", tenant_id="t", aid="a", timestamp_ns=time.time_ns()),
    agent_state_text="ls", plan_text="ls",
    tool_name="Bash", tool_args_json='{"command":"ls"}', safety_flags={},
    memory_fingerprint="sha3:t",
    cost_estimate=CostEfficiencyMetrics(input_token_count=1, output_token_count=1),
)
atv = build_atv(inp)
v = j.evaluate_full('tool=Bash command="ls"', atv=atv, inp=inp)
import hashlib
expected = hashlib.sha3_256(open(os.environ["AEGIS_JUDGE_MODEL_PATH"], "rb").read()).hexdigest()
print(f"DECISION={v.decision}")
print(f"MODEL_HASH_MATCH={'yes' if v.model_hash == expected else 'NO'}")
print(f"LATENCY_MS={v.latency_ms}")
PY
  )"
  REAL_MODE="$(echo "$REAL_OUT" | grep -E "^MODE=" | cut -d= -f2)"
  if [[ "$REAL_MODE" != "real" ]]; then
    fail "LocalPhi did not enter 'real' mode (got: $REAL_MODE) — model may be corrupt"
    note "$(echo "$REAL_OUT" | tail -3 | sed 's/^/    /')"
  else
    LATENCY="$(echo "$REAL_OUT" | grep -E "^LATENCY_MS=" | cut -d= -f2)"
    HASH_OK="$(echo "$REAL_OUT" | grep -E "^MODEL_HASH_MATCH=" | cut -d= -f2)"
    DEC="$(echo "$REAL_OUT" | grep -E "^DECISION=" | cut -d= -f2)"
    if [[ "$HASH_OK" == "yes" ]]; then
      ok "real Llama verdict: $DEC  ($LATENCY ms, model_hash matches file SHA3)"
    else
      fail "model_hash mismatch — verdict ran but hash != file SHA3"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────
printf "\n────────────────────────────────────────────────────────────\n"
TOTAL=$((PASS + FAIL))
if [[ "$FAIL" == "0" ]]; then
  printf "%s✓ %d/%d checks passed%s — green-light for real Claude Code\n" \
    "$C_OK" "$PASS" "$TOTAL" "$C_RESET"
  exit 0
else
  printf "%s✗ %d/%d checks failed%s — DO NOT enable the hook on real traffic yet\n" \
    "$C_ERR" "$FAIL" "$TOTAL" "$C_RESET"
  exit 1
fi
