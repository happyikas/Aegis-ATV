#!/usr/bin/env bash
# Run all 7 incident-response scenarios in sequence.
# Reports per-scenario PASS/FAIL + final summary.

cd "$(dirname "$0")"

SCENARIOS=(
  scenario_a_db_drop.sh
  scenario_b_cost_spike.sh
  scenario_c_prompt_injection.sh
  scenario_d_supply_chain.sh
  scenario_e_insider_drift.sh
  scenario_f_ham_tamper.sh
  scenario_g_multi_agent.sh
)

PASS=()
FAIL=()
START=$(date +%s)

for s in "${SCENARIOS[@]}"; do
  echo
  echo "════════════════════════════════════════════════════════════════════════"
  echo "  RUNNING: $s"
  echo "════════════════════════════════════════════════════════════════════════"
  if bash "$s"; then
    PASS+=("$s")
  else
    FAIL+=("$s")
  fi
done

END=$(date +%s)
ELAPSED=$((END - START))

echo
echo "════════════════════════════════════════════════════════════════════════"
echo "  FINAL SUMMARY (elapsed: ${ELAPSED}s)"
echo "════════════════════════════════════════════════════════════════════════"
echo
echo "  PASSED (${#PASS[@]}):"
for s in "${PASS[@]}"; do echo "    ✓ $s"; done
echo
if [[ ${#FAIL[@]} -gt 0 ]]; then
  echo "  FAILED (${#FAIL[@]}):"
  for s in "${FAIL[@]}"; do echo "    ✗ $s"; done
  echo
  exit 1
fi

echo "  All ${#PASS[@]} scenarios PASSED. ✅"
echo
