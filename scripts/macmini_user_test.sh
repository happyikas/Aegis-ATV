#!/usr/bin/env bash
# Aegis MVP — Mac Mini user test runner.
#
# Usage:
#   ./scripts/macmini_user_test.sh                 # all 7 scenarios, dummy judge
#   ./scripts/macmini_user_test.sh --hybrid        # all 7, hybrid (heuristic+keyword) judge
#   ./scripts/macmini_user_test.sh --scenario 2    # one scenario only
#
# What this does
# --------------
# 1. Sanity-checks the environment (uv + Python 3.11+ + git repo root).
# 2. Creates ./reports/<run-timestamp>/ for this run.
# 3. Runs demo/plugin_scenarios.py with --report-dir pointing at that
#    directory so each scenario emits scenario_<id>_<ts>.{md,json}.
# 4. Prints the path to the report directory at the end.
#
# Exit codes
# ----------
# 0 — all scenarios PASS (or PARTIAL).
# 1 — at least one scenario FAILed; reports still got written.
# 2 — environment problem; nothing was run.

set -euo pipefail

# ── repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── colours (only when stdout is a tty) ───────────────────────────────
if [[ -t 1 ]]; then
  C_BOLD="$(printf '\033[1m')"
  C_OK="$(printf '\033[32m')"
  C_WARN="$(printf '\033[33m')"
  C_ERR="$(printf '\033[31m')"
  C_DIM="$(printf '\033[2m')"
  C_RESET="$(printf '\033[0m')"
else
  C_BOLD=""; C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_RESET=""
fi

say() { printf "%s\n" "$*"; }
ok()  { printf "%s✓%s %s\n" "$C_OK" "$C_RESET" "$*"; }
warn(){ printf "%s!%s %s\n" "$C_WARN" "$C_RESET" "$*"; }
err() { printf "%s✗%s %s\n" "$C_ERR" "$C_RESET" "$*" >&2; }

# ── arg parsing ───────────────────────────────────────────────────────
JUDGE_PROVIDER="${AEGIS_JUDGE_PROVIDER:-dummy}"
SCENARIO="all"
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hybrid)   JUDGE_PROVIDER="hybrid"; shift ;;
    --dummy)    JUDGE_PROVIDER="dummy"; shift ;;
    --scenario) SCENARIO="$2"; shift 2 ;;
    --json)     EXTRA_ARGS+=("--json"); shift ;;
    -h|--help)
      sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *) err "unknown arg: $1"; exit 2 ;;
  esac
done

# ── env check ─────────────────────────────────────────────────────────
say "${C_BOLD}Aegis Mac Mini user test${C_RESET}"
say "${C_DIM}repo: $REPO_ROOT${C_RESET}"
say ""

if ! command -v uv >/dev/null 2>&1; then
  err "uv not found. Install: brew install uv  (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"
  exit 2
fi
ok "uv: $(uv --version)"

PYV="$(uv run python -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null || echo missing)"
if [[ "$PYV" == "missing" ]]; then
  err "uv run python failed — try: uv sync"
  exit 2
fi
ok "python: $PYV"

# ── reports dir ───────────────────────────────────────────────────────
RUN_TS="$(date +%Y%m%d-%H%M%S)"
REPORT_DIR="$REPO_ROOT/reports/$RUN_TS"
mkdir -p "$REPORT_DIR"
ok "reports → $REPORT_DIR"
say ""

# ── sync deps if lock changed (cheap no-op when up to date) ───────────
say "${C_DIM}uv sync (idempotent)…${C_RESET}"
uv sync --quiet
ok "deps in sync"
say ""

# ── run scenarios ─────────────────────────────────────────────────────
say "${C_BOLD}Running scenarios (judge=$JUDGE_PROVIDER, scenario=$SCENARIO)${C_RESET}"
say "──────────────────────────────────────────────────────────────────────"

set +e
AEGIS_EMBEDDING_PROVIDER=dummy \
AEGIS_JUDGE_PROVIDER="$JUDGE_PROVIDER" \
  uv run python demo/plugin_scenarios.py \
    --scenario "$SCENARIO" \
    --report-dir "$REPORT_DIR" \
    "${EXTRA_ARGS[@]}"
RC=$?
set -e

say ""
say "──────────────────────────────────────────────────────────────────────"
N_REPORTS=$(find "$REPORT_DIR" -name 'scenario_*.md' -type f 2>/dev/null | wc -l | tr -d ' ')
ok "$N_REPORTS scenario report(s) saved to:"
say "  ${C_BOLD}$REPORT_DIR${C_RESET}"
say ""
if [[ "$RC" -eq 0 ]]; then
  ok "all scenarios passed (or partial-pass)"
else
  warn "some scenarios FAILed — see reports for details"
fi
say ""
say "${C_DIM}Open a report:${C_RESET}"
say "  open \"$REPORT_DIR\""
say "  cat \"$REPORT_DIR\"/scenario_1_*.md"
say ""
exit "$RC"
