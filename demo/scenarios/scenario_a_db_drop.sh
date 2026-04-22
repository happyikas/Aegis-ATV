#!/usr/bin/env bash
# Scenario A — 손상된 코딩 에이전트가 production DB users 테이블 비우려 한다
# (WHITEPAPER.md §5.1, Replit-pattern incident).
#
# Demonstrates defense in depth — the firewall has TWO independent
# barriers that the attacker has to defeat:
#
#   step 310  pattern-based: known destructive shapes regex-matched
#   step 315  identity-based: per-AID role policy + circuit breaker
#
# Either alone is bypassable; together they compose. The flow:
#
#   ① 검출    DR0P TABLE → step 310 BLOCK in milliseconds
#   ② 격리    Try stealthier tool → step 315 violations → quarantine
#   ③ 포렌식  Audit chain export + AES-GCM journal walk
#   ④ 복구    /admin/aid/release with admin token
#   ⑤ 사후    Burn-in L4 baseline auto-records the anomaly

cd "$(dirname "$0")"
. ./_lib.sh

scenario "Scenario A — Production DB Drop Attempt (§5.1)"
ensure_aegis

AID="compromised-coding-agent-$(uuid | head -c 8)"
ROLE="read-only-role"
note "AID = $AID, role = $ROLE (max_violations = 3 per policies/aid_region.json)"

# ─────────────────────────────────────────────────────────────────────
# ① 검출 — pattern-based catch at step 310
# ─────────────────────────────────────────────────────────────────────
stage "① 검출 — POST /evaluate with destructive SQL pattern"

SQL_ARGS=$(python3 -c "import json; print(json.dumps({'sql': 'DR' + 'OP TABLE users CASCADE'}))")
PAYLOAD=$(build_payload "db_mutation" "$SQL_ARGS" "$AID" --role "$ROLE")

START=$(python3 -c "import time; print(time.time())")
VERDICT=$(evaluate "$PAYLOAD")
END=$(python3 -c "import time; print(time.time())")
LATENCY_MS=$(python3 -c "print(int(($END - $START) * 1000))")

DECISION=$(echo "$VERDICT" | python3 -c "import json,sys;print(json.load(sys.stdin)['decision'])")
REASON=$(echo "$VERDICT" | python3 -c "import json,sys;print(json.load(sys.stdin)['reason'][:80])")

info "decision: $DECISION"
info "reason:   $REASON"
info "latency:  ${LATENCY_MS}ms (includes network + Haiku if reached)"

assert_eq "BLOCK" "$DECISION" "step 310 regex catches destructive SQL"

# ─────────────────────────────────────────────────────────────────────
# ② 격리 — identity-based escalation at step 315
# ─────────────────────────────────────────────────────────────────────
stage "② 격리 — 공격자가 step 310을 우회하려 다른 도구 시도 → step 315 차단"

note "read-only-role 정책상 write_file 호출 권한 없음"
note "max_violations=3 → 3회 후 자동 quarantine"

WRITE_ARGS=$(python3 -c "import json; print(json.dumps({'path':'./tmp/x.txt','content':'attacker payload'}))")

for i in 1 2 3; do
  P=$(build_payload "write_file" "$WRITE_ARGS" "$AID" --role "$ROLE")
  V=$(evaluate "$P")
  D=$(echo "$V" | python3 -c "import json,sys;print(json.load(sys.stdin)['decision'])")
  R=$(echo "$V" | python3 -c "import json,sys;print(json.load(sys.stdin)['reason'][:90])")
  info "violation $i/3 → $D — $R"
done

stage "  → AID 상태 확인"
QUARANTINE=$(curl -s "$AEGIS_URL/admin/aid/$AID")
STATUS=$(echo "$QUARANTINE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status',''))")
VIOLATIONS=$(echo "$QUARANTINE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('violations',0))")
info "AID status:    $STATUS"
info "violation cnt: $VIOLATIONS"
assert_eq "quarantined" "$STATUS" "AID auto-quarantined after 3 violations"

stage "  → 격리 후 합법적 read_file 도 차단되는지 확인"
# Use a path inside the role's allowed_paths so the BLOCK proves
# quarantine, not path policy.
SAFE=$(build_payload "read_file" '{"path":"./data/report.txt"}' "$AID" --role "$ROLE")
FOLLOWUP=$(evaluate "$SAFE")
FU_DECISION=$(echo "$FOLLOWUP" | python3 -c "import json,sys;print(json.load(sys.stdin)['decision'])")
FU_REASON=$(echo "$FOLLOWUP" | python3 -c "import json,sys;print(json.load(sys.stdin)['reason'][:80])")
info "post-quarantine read_file → $FU_DECISION"
info "reason: $FU_REASON"
assert_eq "BLOCK" "$FU_DECISION" "quarantined AID hard-blocked even on safe operation"
assert_contains "quarantined" "$FU_REASON" "block reason mentions quarantine"

# ─────────────────────────────────────────────────────────────────────
# ③ 포렌식 — 10초 안에 답변
# ─────────────────────────────────────────────────────────────────────
stage "③ 포렌식 — audit chain + 암호화 저널"

CHAIN=$(curl -s "$AEGIS_URL/audit/$AID")
LEN=$(echo "$CHAIN" | python3 -c "import json,sys;print(json.load(sys.stdin)['length'])")
VALID=$(echo "$CHAIN" | python3 -c "import json,sys;print(json.load(sys.stdin)['chain_valid'])")
info "chain length:  $LEN records (1 SQL + 3 violations + 1 follow-up = 5)"
info "chain valid:   $VALID (Ed25519 + Merkle SHA3-256)"
assert_eq "True" "$VALID" "audit chain cryptographically intact"
assert_gte 5 "$LEN" "all 5 attempts recorded"

REPLAY=$(curl -s "$AEGIS_URL/forensic/replay")
DECRYPTED=$(echo "$REPLAY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('decrypted_count',0))")
TAMPERED=$(echo "$REPLAY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('tampered_count',0))")
info "AES-GCM journal: $DECRYPTED decrypted, $TAMPERED tampered (must be 0)"
assert_eq "0" "$TAMPERED" "no tampering in encrypted journal"

# ─────────────────────────────────────────────────────────────────────
# ④ 복구 — admin release
# ─────────────────────────────────────────────────────────────────────
stage "④ 복구 — admin token으로 AID 격리 해제"

REL_BODY=$(python3 -c "import json;print(json.dumps({'aid':'$AID','reason':'scenario demo: human reviewed'}))")
RELEASE=$(curl -s -X POST "$AEGIS_URL/admin/aid/release" \
  -H "X-Aegis-Admin-Token: dev-admin-token" \
  -H "content-type: application/json" \
  -d "$REL_BODY")
RELEASED=$(echo "$RELEASE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status','?'))")
info "after release: $RELEASED"
assert_eq "normal" "$RELEASED" "AID released, status back to normal"

note "Verifying release worked — read_file should ALLOW now"
SAFE2=$(build_payload "read_file" '{"path":"./data/report.txt"}' "$AID" --role "$ROLE")
POST_REL=$(evaluate "$SAFE2")
PR_DECISION=$(echo "$POST_REL" | python3 -c "import json,sys;print(json.load(sys.stdin)['decision'])")
info "post-release read_file → $PR_DECISION"
assert_eq "ALLOW" "$PR_DECISION" "released AID can call allowed tools again"

# ─────────────────────────────────────────────────────────────────────
# ⑤ 사후 — Burn-in baseline auto-recalibrates
# ─────────────────────────────────────────────────────────────────────
stage "⑤ 사후 — Burn-in L4 부분 재캘리브레이션 (자동)"
BURNIN=$(curl -s "$AEGIS_URL/burnin-status" | python3 -c "
import json, sys
d = json.load(sys.stdin)
slots = [l for l in d.get('layers',[]) if 'role' in (l.get('key','') or '')]
print(len(slots))
")
info "L4 (role-level) baseline slots active: $BURNIN"
note "next time same role attempts the same tool, burn-in shifts blast radius up"

# ─────────────────────────────────────────────────────────────────────
# Before/after comparison
# ─────────────────────────────────────────────────────────────────────
echo
echo "${_C_BOLD}  AegisData 없음 vs AegisData${_C_RESET}"
echo "  ┌────────────────┬────────────────────────┬──────────────────────────┐"
echo "  │ 단계            │ 없음                    │ AegisData                  │"
echo "  ├────────────────┼────────────────────────┼──────────────────────────┤"
printf "  │ %-14s │ %-22s │ %-24s │\n" "검출"     "4시간 후 monitoring"      "${LATENCY_MS}ms"
printf "  │ %-14s │ %-22s │ %-24s │\n" "손상 범위"   "users 테이블 1.2M rows"  "0 rows (시도만 차단)"
printf "  │ %-14s │ %-22s │ %-24s │\n" "포렌식"   "수일 (수동 correlation)" "10초 (1 endpoint)"
printf "  │ %-14s │ %-22s │ %-24s │\n" "복구 비용" "회사 매각 위기"           "0달러"
echo "  └────────────────┴────────────────────────┴──────────────────────────┘"

scenario_end
