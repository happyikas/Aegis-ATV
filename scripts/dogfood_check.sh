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
# Find a *judge* GGUF (skip embedding GGUFs whose names start with "bge-").
# Prefer the env-configured one if set; otherwise pick whichever judge
# GGUF is in models/. Phi-3.5-mini wins over Llama-1B when both are
# present (it's the upgrade path users opt into for accuracy).
if [[ -n "${AEGIS_JUDGE_MODEL_PATH:-}" && -f "$AEGIS_JUDGE_MODEL_PATH" ]]; then
  MODEL_FILE="$AEGIS_JUDGE_MODEL_PATH"
else
  MODEL_FILE="$(ls "$REPO_ROOT"/models/Phi-3.5-mini*.gguf 2>/dev/null | head -1)"
  if [[ -z "$MODEL_FILE" ]]; then
    MODEL_FILE="$(ls "$REPO_ROOT"/models/*.gguf 2>/dev/null | grep -v -i 'bge-' | head -1)"
  fi
fi
if [[ "$LLAMA_CHECK" != "ok" ]]; then
  note "skipped — llama-cpp-python not installed.  uv sync --extra local-llm"
elif [[ -z "$MODEL_FILE" ]]; then
  note "skipped — no judge GGUF in $REPO_ROOT/models/.  uv run aegis pull-model"
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
    MODEL_NAME="$(basename "$MODEL_FILE" .gguf)"
    if [[ "$HASH_OK" == "yes" ]]; then
      ok "real verdict: $DEC  ($MODEL_NAME, ${LATENCY} ms, model_hash matches file SHA3)"
      # Cold-subprocess timeout warning. Each PreToolUse fires a fresh
      # python3, so the LLM is loaded from scratch every time the
      # hybrid cascade escalates past M13. Phi-3.5-mini at ~6.5 s
      # cold-loads exceed Claude Code's 5 s hook timeout when the LLM
      # *is* invoked (that's the gray-zone ~10 % of calls).
      LATENCY_INT="${LATENCY%.*}"
      if [[ "${LATENCY_INT:-0}" -gt 4500 ]]; then
        printf "  %s!%s cold-load %s ms approaches the 5 s Claude Code hook timeout.\n" \
          "$C_WARN" "$C_RESET" "$LATENCY"
        printf "  %s   consider llama-3.2-1b for a fast judge, or use %s as a quality-first opt-in.\n" \
          "$C_DIM" "$MODEL_NAME"
        printf "%s\n" "$C_RESET"
      fi
    else
      fail "model_hash mismatch — verdict ran but hash != file SHA3"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────
# 10. Real local embedding (BGE) — only if BGE GGUF + llama-cpp present
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[10] Real BGE embedding%s\n" "$C_BOLD" "$C_RESET"
BGE_FILE="$(ls "$REPO_ROOT"/models/bge-*.gguf 2>/dev/null | head -1)"
if [[ "$LLAMA_CHECK" != "ok" ]]; then
  note "skipped — llama-cpp-python not installed."
elif [[ -z "$BGE_FILE" ]]; then
  note "skipped — no BGE GGUF in $REPO_ROOT/models/.  uv run aegis pull-model --model bge-base-en"
else
  BGE_OUT="$(AEGIS_EMBEDDING_PROVIDER=bge-local \
             AEGIS_EMBEDDING_MODEL_PATH="$BGE_FILE" \
             "$PYTHON" - <<'PY' 2>&1
import time
import numpy as np
from aegis.atv.embeddings import BGELocalEmbedding, DummyEmbedding, reset_bge_cache

reset_bge_cache()
bge = BGELocalEmbedding()
dum = DummyEmbedding()

# Two semantically-similar phrases vs one different — BGE should
# show clearly higher similarity for the matched pair.
texts = [
    "delete production database",          # destructive
    "drop production tables",              # also destructive (similar)
    "list files in temp directory",        # benign
]
t0 = time.perf_counter_ns()
v_real = [bge.embed(t, 768) for t in texts]
real_ms = (time.perf_counter_ns() - t0) / 1_000_000

v_dum = [dum.embed(t, 768) for t in texts]

sim_real_pos = float(np.dot(v_real[0], v_real[1]))   # destructive ↔ destructive
sim_real_neg = float(np.dot(v_real[0], v_real[2]))   # destructive ↔ benign
sim_dum_pos  = float(np.dot(v_dum[0], v_dum[1]))
sim_dum_neg  = float(np.dot(v_dum[0], v_dum[2]))

print(f"REAL_MS={real_ms:.0f}")
print(f"REAL_POS={sim_real_pos:.3f}")
print(f"REAL_NEG={sim_real_neg:.3f}")
print(f"DUM_POS={sim_dum_pos:.3f}")
print(f"DUM_NEG={sim_dum_neg:.3f}")
# REAL provider is meaningful when sim(destructive, destructive) is
# meaningfully higher than sim(destructive, benign).
print(f"SEMANTIC={'yes' if sim_real_pos > sim_real_neg + 0.10 else 'NO'}")
PY
  )"
  if echo "$BGE_OUT" | grep -q '^SEMANTIC=yes'; then
    POS="$(echo "$BGE_OUT" | grep '^REAL_POS=' | cut -d= -f2)"
    NEG="$(echo "$BGE_OUT" | grep '^REAL_NEG=' | cut -d= -f2)"
    MS="$(echo "$BGE_OUT" | grep '^REAL_MS=' | cut -d= -f2)"
    ok "real BGE: cos(destructive, destructive)=$POS > cos(destructive, benign)=$NEG  (${MS} ms)"
  else
    fail "BGE produced no semantic signal — is the GGUF corrupt?"
    note "$(echo "$BGE_OUT" | tail -5 | sed 's/^/    /')"
  fi
fi

# ─────────────────────────────────────────────────────────────────────
# 11. Step340 RAG case-memory retrieval (only if memory + BGE present)
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[11] Step340 RAG retrieval%s\n" "$C_BOLD" "$C_RESET"
MEM_FILE="$REPO_ROOT/models/case_memory_v1.npz"
if [[ "$LLAMA_CHECK" != "ok" ]]; then
  note "skipped — llama-cpp-python not installed."
elif [[ -z "$BGE_FILE" ]]; then
  note "skipped — no BGE GGUF (cosines would be meaningless without it)."
elif [[ ! -f "$MEM_FILE" ]]; then
  note "skipped — no case memory at $MEM_FILE.  uv run aegis case-memory build"
else
  RAG_OUT="$(AEGIS_EMBEDDING_PROVIDER=bge-local \
             AEGIS_EMBEDDING_MODEL_PATH="$BGE_FILE" \
             "$PYTHON" - <<'PY' 2>&1
import time
import numpy as np
from aegis.config import settings as _settings
_settings.aegis_embedding_provider = "bge-local"
from aegis.judge.case_memory import (
    load_default_memory, reset_memory_cache, format_cases_for_prompt,
)
from aegis.atv.embeddings import BGELocalEmbedding, reset_bge_cache

reset_memory_cache(); reset_bge_cache()
mem = load_default_memory()
print(f"N={mem.n}")
if mem.is_empty:
    raise SystemExit(0)

# Query: a destructive Bash. The memory should retrieve other
# destructive examples with high cosine.
bge = BGELocalEmbedding()
q_text = "user wants to clean up disk space  | tool=Bash  args={\"command\":\"rm -rf /var/log\"}"
q = bge.embed(q_text, 768)
hits = mem.search(q, k=3)
print(f"K={len(hits)}")
if hits:
    print(f"TOP_COS={hits[0].similarity:.3f}")
    print(f"TOP_LABEL={hits[0].label}")
    # Mostly-malicious top-3: success.
    n_malicious = sum(1 for h in hits if h.label in ("BLOCK", "REQUIRE_APPROVAL"))
    print(f"N_MALICIOUS={n_malicious}")
PY
  )"
  N="$(echo "$RAG_OUT" | grep '^N=' | cut -d= -f2)"
  K="$(echo "$RAG_OUT" | grep '^K=' | cut -d= -f2)"
  TOP_COS="$(echo "$RAG_OUT" | grep '^TOP_COS=' | cut -d= -f2)"
  TOP_LABEL="$(echo "$RAG_OUT" | grep '^TOP_LABEL=' | cut -d= -f2)"
  N_MAL="$(echo "$RAG_OUT" | grep '^N_MALICIOUS=' | cut -d= -f2)"
  if [[ -n "$N" && "$N" -gt "0" && -n "$K" && "$K" -gt "0" \
        && -n "$TOP_COS" && -n "$N_MAL" && "$N_MAL" -ge "2" ]]; then
    ok "memory n=$N, retrieved $K (top cos=$TOP_COS, label=$TOP_LABEL, $N_MAL/3 malicious)"
  else
    fail "RAG retrieval did not return malicious neighbours for a destructive query"
    note "$(echo "$RAG_OUT" | tail -5 | sed 's/^/    /')"
  fi
fi

# ─────────────────────────────────────────────────────────────────────
# 12. Session-behavioural drift (BGE-derived topic_drift signal)
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[12] Session drift detection%s\n" "$C_BOLD" "$C_RESET"
if [[ "$LLAMA_CHECK" != "ok" ]]; then
  note "skipped — llama-cpp-python not installed."
elif [[ -z "$BGE_FILE" ]]; then
  note "skipped — no BGE GGUF (cosine drift would be SHA3 noise)."
else
  DRIFT_OUT="$(AEGIS_SESSION_DIR=$(mktemp -d)/sessions \
               AEGIS_EMBEDDING_PROVIDER=bge-local \
               AEGIS_EMBEDDING_MODEL_PATH="$BGE_FILE" \
               "$PYTHON" - <<'PY' 2>&1
from aegis.atv.embeddings import get_provider, reset_bge_cache
from aegis.atv.session_drift import update_and_score
reset_bge_cache()
prov = get_provider()
sid = "dogfood-drift-test"

# Anchor: debugging session
anchor_text = "user wants to debug a python error in main.py"
attack_text = "rm -rf /var/log/* && truncate database tables"

emb1 = prov.embed(anchor_text, 768)
emb2 = prov.embed(attack_text, 768)

s1 = update_and_score(session_id=sid, current_embedding=emb1, current_plan_len=50)
s2 = update_and_score(session_id=sid, current_embedding=emb2, current_plan_len=50)

print(f"ANCHOR_DRIFT={s1.topic_drift:.3f}")
print(f"DRIFT_AT_ATTACK={s2.topic_drift:.3f}")
print(f"IS_ANCHOR_FIRST={s1.is_anchor_call}")
PY
  )"
  ANCHOR_DRIFT="$(echo "$DRIFT_OUT" | grep '^ANCHOR_DRIFT=' | cut -d= -f2)"
  ATTACK_DRIFT="$(echo "$DRIFT_OUT" | grep '^DRIFT_AT_ATTACK=' | cut -d= -f2)"
  IS_ANCHOR="$(echo "$DRIFT_OUT" | grep '^IS_ANCHOR_FIRST=' | cut -d= -f2)"
  # Anchor call must be 0 drift; attack call must show meaningful drift
  # (>= 0.30 is the cosine retrieval threshold = unambiguous semantic shift).
  if [[ "$IS_ANCHOR" == "True" ]] && \
     awk "BEGIN { exit !($ANCHOR_DRIFT == 0) }" 2>/dev/null && \
     awk "BEGIN { exit !($ATTACK_DRIFT >= 0.30) }" 2>/dev/null; then
    ok "anchor drift=$ANCHOR_DRIFT (is_anchor=$IS_ANCHOR), attack drift=$ATTACK_DRIFT (≥ 0.30)"
  else
    fail "drift signal absent: anchor=$ANCHOR_DRIFT (is_anchor=$IS_ANCHOR), attack=$ATTACK_DRIFT"
    note "$(echo "$DRIFT_OUT" | tail -5 | sed 's/^/    /')"
  fi
fi

# ─────────────────────────────────────────────────────────────────────
# 13. Audit-record enrichment (`aegis report --explain` precondition)
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[13] Audit-record enrichment%s\n" "$C_BOLD" "$C_RESET"
# Reuses the audit log step [6] populated. The record from step [3]
# (rm -rf BLOCK) should now carry an `explain` block with step traces
# and M13 attribution.
EXPLAIN_OUT="$(uv run aegis report --audit "$TEST_AUDIT" --explain LAST 2>&1)"
if echo "$EXPLAIN_OUT" | grep -q "Decision Explanation" \
   && echo "$EXPLAIN_OUT" | grep -q "M13 attribution"; then
  ok "explain renders header + M13 attribution + step traces"
else
  if echo "$EXPLAIN_OUT" | grep -q "no PreToolUse decisions"; then
    note "skipped — audit log empty (step [3] should have populated it)"
  else
    fail "explain output missing required sections"
    note "$(echo "$EXPLAIN_OUT" | head -3 | sed 's/^/    /')"
  fi
fi

# ─────────────────────────────────────────────────────────────────────
# 14. Audit-log rotation + cross-file verify
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[14] Audit rotation + verify%s\n" "$C_BOLD" "$C_RESET"
ROTATION_TMPDIR="$(mktemp -d)"
ROT_OUT="$(AEGIS_AUDIT_MAX_BYTES=2000 \
           AEGIS_AUDIT_MAX_ROTATIONS=3 \
           "$PYTHON" - <<PY 2>&1
from pathlib import Path
from aegis.audit.local_chain import append, verify_chain
from aegis.audit.rotation import list_rotation_chain

p = Path("$ROTATION_TMPDIR/audit.jsonl")
for i in range(50):
    append(p, {"i": i, "msg": f"r{i:04d}"})

chain = list_rotation_chain(p)
print(f"FILES={len(chain)}")
ok, broken, total = verify_chain(p)
print(f"VERIFY_OK={ok}")
print(f"TOTAL={total}")
PY
)"
rm -rf "$ROTATION_TMPDIR"
N_FILES="$(echo "$ROT_OUT" | grep '^FILES=' | cut -d= -f2)"
VERIFY_OK="$(echo "$ROT_OUT" | grep '^VERIFY_OK=' | cut -d= -f2)"
TOTAL="$(echo "$ROT_OUT" | grep '^TOTAL=' | cut -d= -f2)"
if [[ "$VERIFY_OK" == "True" && -n "$N_FILES" && "$N_FILES" -gt "1" \
      && -n "$TOTAL" && "$TOTAL" -gt "0" ]]; then
  ok "rotated $N_FILES files, $TOTAL records, chain verified across boundaries"
else
  fail "rotation/verify failed: files=$N_FILES verify=$VERIFY_OK total=$TOTAL"
  note "$(echo "$ROT_OUT" | tail -3 | sed 's/^/    /')"
fi

# ─────────────────────────────────────────────────────────────────────
# 15. Install ↔ uninstall roundtrip (sandboxed)
# ─────────────────────────────────────────────────────────────────────
printf "\n%s[15] Install ↔ uninstall roundtrip%s  %s(sandboxed)%s\n" \
  "$C_BOLD" "$C_RESET" "$C_DIM" "$C_RESET"
SANDBOX2="$(mktemp -d)"
mkdir -p "$SANDBOX2/.claude"
HOME_REAL="$HOME"
# Pre-existing third-party hook the user might have. Must survive
# the install + uninstall round-trip.
"$PYTHON" - <<PY
import json
from pathlib import Path
p = Path("$SANDBOX2/.claude/settings.json")
p.write_text(json.dumps({
    "hooks": {
        "PreToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command",
             "command": "/usr/local/bin/prettier-check"}]},
        ],
    },
}, indent=2))
PY
HOME="$SANDBOX2" uv run aegis install --mode local --judge dummy >/dev/null 2>&1
INSTALL_RC=$?
HOME="$SANDBOX2" uv run aegis uninstall --no-backup >/dev/null 2>&1
UNINSTALL_RC=$?
# grep -c emits "0" AND exits 1 on zero matches → ``|| echo 0`` would
# duplicate. Capture the body first, then default to 0 if grep had to fail.
N_AEGIS="$(grep -c 'aegis_local_hook.py\|tools/hooks/post_tool.py\|tools/hooks/session_end.py' \
  "$SANDBOX2/.claude/settings.json" 2>/dev/null)"
N_AEGIS="${N_AEGIS:-0}"
N_THIRD_PARTY="$(grep -c 'prettier-check' "$SANDBOX2/.claude/settings.json" 2>/dev/null)"
N_THIRD_PARTY="${N_THIRD_PARTY:-0}"
HOME="$HOME_REAL"
rm -rf "$SANDBOX2"
if [[ "$INSTALL_RC" == "0" && "$UNINSTALL_RC" == "0" \
      && "$N_AEGIS" == "0" && "$N_THIRD_PARTY" == "1" ]]; then
  ok "round-trip clean: 0 Aegis hooks remain, 1 third-party hook preserved"
else
  fail "round-trip broken: install=$INSTALL_RC uninstall=$UNINSTALL_RC aegis=$N_AEGIS third-party=$N_THIRD_PARTY"
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
