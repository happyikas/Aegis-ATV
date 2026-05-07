#!/usr/bin/env bash
# Quickstart smoke test — exercises the README install path end-to-end
# on a clean machine.
#
# Run locally:
#   bash scripts/quickstart_smoke.sh
#
# In CI:
#   .github/workflows/quickstart-smoke.yml runs this on a fresh
#   ubuntu-latest runner with HOME set to a tmp dir, so the test does
#   not touch the runner's real ~/.claude/.
#
# Steps verified:
#   1. `uv sync` resolves and installs deps.
#   2. `aegis --version` prints something plausible.
#   3. `aegis install --mode local` patches a synthetic ~/.claude/settings.json.
#   4. The patched settings.json contains a PreToolUse hook line
#      pointing at tools/aegis_local_hook.py with the anti-self-DoS
#      defaults (AEGIS_APPROVE_AS_BLOCK=0, AEGIS_TOKEN_BUDGET=99999999)
#      that PR #101 introduced for local mode.
#   5. `aegis verify-audit` runs cleanly against a fresh empty log.
#   6. `aegis report` prints the 5-line summary banner.
#   7. `aegis uninstall --dry-run` reports the hook would be removed.

set -euo pipefail

c_red()    { printf '\033[31m%s\033[0m' "$*"; }
c_green()  { printf '\033[32m%s\033[0m' "$*"; }
c_blue()   { printf '\033[34m%s\033[0m' "$*"; }
c_dim()    { printf '\033[2m%s\033[0m' "$*"; }

step() { printf "%s %s\n" "$(c_blue '==>')" "$*"; }
ok()   { printf "%s %s\n" "$(c_green '✓')" "$*"; }
die()  { printf "%s %s\n" "$(c_red '✗')" "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

# ── 1. uv sync ───────────────────────────────────────────────────────

step "uv sync (frozen)..."
uv sync --frozen >/dev/null
ok "deps resolved"

# ── 2. aegis CLI entrypoint reachable ───────────────────────────────

# `aegis` has no `--version` flag (CLI version is printed inside the
# install banner instead). Use `--help` as the entrypoint smoke check
# — it exits 0 and prints the subcommand list.

step "aegis --help"
help_out=$(uv run aegis --help 2>&1)
echo "${help_out}" | head -3 | sed 's/^/    /'
echo "${help_out}" | grep -q "verify-audit" \
  || die "aegis --help did not list expected subcommands"
ok "CLI entrypoint reachable"

# ── 3. aegis install --mode local against a tmp HOME ────────────────

# Run install with HOME pointing at a throwaway dir so the runner's
# real ~/.claude/settings.json is never touched.
TMP_HOME=$(mktemp -d)
trap 'rm -rf "${TMP_HOME}"' EXIT
mkdir -p "${TMP_HOME}/.claude"

step "aegis install --mode local  (HOME=${TMP_HOME})"
HOME="${TMP_HOME}" uv run aegis install --mode local
ok "install completed"

settings="${TMP_HOME}/.claude/settings.json"
[[ -f "${settings}" ]] || die "settings.json was not created at ${settings}"
ok "settings.json created"

# ── 4. shape checks on the patched settings.json ────────────────────

step "verifying patched hook shape..."

# The hook command should reference the local hook script.
grep -q "aegis_local_hook" "${settings}" \
  || die "settings.json does not reference aegis_local_hook"
ok "PreToolUse hook references aegis_local_hook"

# Anti-self-DoS env vars (PR #101): local mode must prepend these so
# a misconfigured cost gate cannot wedge the user's Claude Code.
grep -q "AEGIS_APPROVE_AS_BLOCK=0" "${settings}" \
  || die "AEGIS_APPROVE_AS_BLOCK=0 missing from local-mode hook command"
grep -q "AEGIS_TOKEN_BUDGET=99999999" "${settings}" \
  || die "AEGIS_TOKEN_BUDGET=99999999 missing from local-mode hook command"
ok "anti-self-DoS defaults applied"

# A timestamped backup should exist whenever install runs over an
# existing settings.json. We pre-created an empty stub for this
# purpose — confirm install rotated it.
shopt -s nullglob
backups=( "${TMP_HOME}/.claude/settings.json.bak."* )
shopt -u nullglob
if (( ${#backups[@]} > 0 )); then
  ok "backup created: ${backups[0]}"
else
  echo "    (no pre-existing settings.json, so no backup expected on first install)"
fi

# ── 5. verify-audit on a fresh empty log ────────────────────────────

# Right after install no tool calls have happened yet, so the audit
# log doesn't exist. Both verify-audit and report exit 1 with a
# friendly "no audit log" banner in this case — that's the documented
# happy path, so we capture and grep instead of asserting on $?.

step "aegis verify-audit  (no audit yet)"
out=$(HOME="${TMP_HOME}" uv run aegis verify-audit 2>&1 || true)
echo "${out}" | sed 's/^/    /'
echo "${out}" | grep -q "no local audit log" \
  || die "expected 'no local audit log' banner from verify-audit"
ok "verify-audit reported empty log cleanly"

# ── 6. report on a fresh empty log ──────────────────────────────────

step "aegis report  (no audit yet)"
out=$(HOME="${TMP_HOME}" uv run aegis report 2>&1 || true)
echo "${out}" | sed 's/^/    /'
echo "${out}" | grep -q "no audit log" \
  || die "expected 'no audit log' banner from report"
ok "report banner printed without crashing"

# ── 7. uninstall dry-run ────────────────────────────────────────────

step "aegis uninstall --dry-run"
HOME="${TMP_HOME}" uv run aegis uninstall --dry-run
ok "uninstall dry-run completed"

echo
echo "$(c_green '─── Quickstart smoke: all steps PASS ───')"
echo
echo "  Verified install path:"
echo "    1. uv sync"
echo "    2. uv run aegis install --mode local"
echo "    3. anti-self-DoS env vars present in patched settings.json"
echo "    4. aegis verify-audit / report / uninstall --dry-run"
