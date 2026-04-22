#!/usr/bin/env bash
# Scenario D — 공급망 공격 (WHITEPAPER §5.4, AWS Q Extension pattern).
#
# Demonstrates that code attestation (M7) detects config / source
# tampering — the burn_in_id changes when policy files or source
# files change, and downstream all AIDs see a new burn_in_id in their
# audit headers, forcing Burn-in baselines to recalibrate.

cd "$(dirname "$0")"
. ./_lib.sh

scenario "Scenario D — Supply Chain Attack Detection (§5.4)"
ensure_aegis

# ─────────────────────────────────────────────────────────────────────
# ① 검출 — capture current burn_in_id
# ─────────────────────────────────────────────────────────────────────
stage "① 사전 — 현재 burn_in_id 기록"

BEFORE=$(curl -s "$AEGIS_URL/attestation")
BEFORE_ID=$(echo "$BEFORE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('burn_in_id',''))")
BEFORE_L4=$(echo "$BEFORE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('layers',{}).get('L4_config',{}).get('hash',''))")

info "현재 burn_in_id:    $BEFORE_ID"
info "현재 L4_config hash: ${BEFORE_L4:0:24}..."

# Place a representative tool call so we have a "before" record.
P=$(build_payload "read_file" '{"path":"./data/report.txt"}' "supply-chain-test" --tenant "demo-tenant")
V=$(evaluate "$P")
BEFORE_BURNIN_HEADER=$(curl -s "$AEGIS_URL/audit/supply-chain-test" | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('chain'):
    print(d['chain'][-1]['payload']['header'].get('burn_in_id',''))
else:
    print('')
")
info "audit header burn_in_id: ${BEFORE_BURNIN_HEADER:0:24}..."
assert_eq "$BEFORE_ID" "$BEFORE_BURNIN_HEADER" "audit headers carry the burn_in_id"

# ─────────────────────────────────────────────────────────────────────
# ② 시뮬레이션 — modify a policy file (simulating supply-chain tamper)
# ─────────────────────────────────────────────────────────────────────
stage "② 검출 — policy 변경 시뮬레이션 + 서비스 재시작"

note "We add a benign comment to policies/aid_region.json to simulate"
note "a malicious config tamper. Real attack would inject permissive rules."

ROOT=$(git rev-parse --show-toplevel)

# Modify the policy file *inside the running container* to simulate
# a supply-chain attack where an attacker has runtime access. (Host-
# side modifications don't propagate because policies/ is baked into
# the image at build time.)
INNER_PATH="/app/policies/aid_region.json"
note "backing up container's $INNER_PATH..."
docker exec aegis-mvp cp "$INNER_PATH" "${INNER_PATH}.bak" 2>/dev/null
trap '
docker exec aegis-mvp cp '"${INNER_PATH}"'.bak '"${INNER_PATH}"' 2>/dev/null;
docker exec aegis-mvp rm -f '"${INNER_PATH}"'.bak 2>/dev/null;
(cd '"${ROOT}"' && docker compose restart aegis 2>/dev/null) >/dev/null 2>&1
' EXIT

# Inject a benign tamper marker into the inner file
docker exec aegis-mvp python3 -c "
import json
p = '$INNER_PATH'
data = json.load(open(p))
data['_supply_chain_tamper_marker'] = 'INJECTED_$(date +%s)'
open(p,'w').write(json.dumps(data, indent=2))
print('  policy file modified inside container')
"

note "Restarting Aegis to recompute burn_in_id..."
(cd "$ROOT" && docker compose restart aegis >/dev/null 2>&1)
sleep 3
until curl -sf "$AEGIS_URL/healthz" >/dev/null; do sleep 1; done

AFTER=$(curl -s "$AEGIS_URL/attestation")
AFTER_ID=$(echo "$AFTER" | python3 -c "import json,sys;print(json.load(sys.stdin).get('burn_in_id',''))")
AFTER_L4=$(echo "$AFTER" | python3 -c "import json,sys;print(json.load(sys.stdin).get('layers',{}).get('L4_config',{}).get('hash',''))")

info "변경 후 burn_in_id:    $AFTER_ID"
info "변경 후 L4_config hash: ${AFTER_L4:0:24}..."

if [[ "$BEFORE_ID" != "$AFTER_ID" ]]; then
  pass "burn_in_id가 변경됨 — supply chain tampering 자동 감지"
else
  fail "burn_in_id가 변하지 않음 — code attestation이 변경을 못 잡음"
fi

if [[ "$BEFORE_L4" != "$AFTER_L4" ]]; then
  pass "L4_config hash 변경됨 — 정확히 어디가 변경됐는지 식별 가능"
else
  fail "L4 hash가 변하지 않음"
fi

# ─────────────────────────────────────────────────────────────────────
# ③ 격리 — new burn_in_id environment forces all AIDs to revalidate
# ─────────────────────────────────────────────────────────────────────
stage "③ 격리 — 새 burn_in_id 환경에서 후속 호출"

P2=$(build_payload "read_file" '{"path":"./data/report.txt"}' "supply-chain-test" --tenant "demo-tenant")
V2=$(evaluate "$P2")
NEW_BURNIN_HEADER=$(curl -s "$AEGIS_URL/audit/supply-chain-test" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d['chain'][-1]['payload']['header'].get('burn_in_id',''))
")
info "새 audit record의 burn_in_id: ${NEW_BURNIN_HEADER:0:24}..."
assert_eq "$AFTER_ID" "$NEW_BURNIN_HEADER" "new records carry new burn_in_id"

# ─────────────────────────────────────────────────────────────────────
# ④ 포렌식 — chain spans two burn_in_ids
# ─────────────────────────────────────────────────────────────────────
stage "④ 포렌식 — audit chain의 burn_in_id boundary로 어느 record가 어느 빌드에서 만들어졌는지 분류"

CHAIN=$(curl -s "$AEGIS_URL/audit/supply-chain-test")
ANALYSIS=$(echo "$CHAIN" | python3 -c "
import json, sys
from collections import Counter
d = json.load(sys.stdin)
c = Counter()
for rec in d.get('chain', []):
    bid = rec['payload']['header'].get('burn_in_id','')[:16] + '...'
    c[bid] += 1
for bid, n in sorted(c.items()):
    print(f'  {bid}: {n} records')
")
echo "$ANALYSIS" | sed 's/^/      /'

DISTINCT=$(echo "$ANALYSIS" | wc -l | tr -d ' ')
assert_gte 2 "$DISTINCT" "audit chain spans 2+ burn_in_ids — tamper boundary visible"

# ─────────────────────────────────────────────────────────────────────
# ⑤ 복구 — rollback the tamper
# ─────────────────────────────────────────────────────────────────────
stage "⑤ 복구 — policy rollback + 서비스 재시작"

docker exec aegis-mvp cp "${INNER_PATH}.bak" "$INNER_PATH" 2>/dev/null
docker exec aegis-mvp rm -f "${INNER_PATH}.bak" 2>/dev/null
note "policy 원본으로 rollback"
(cd "$ROOT" && docker compose restart aegis >/dev/null 2>&1)
sleep 3
until curl -sf "$AEGIS_URL/healthz" >/dev/null; do sleep 1; done

RESTORED=$(curl -s "$AEGIS_URL/attestation")
RESTORED_ID=$(echo "$RESTORED" | python3 -c "import json,sys;print(json.load(sys.stdin).get('burn_in_id',''))")
info "rollback 후 burn_in_id: ${RESTORED_ID:0:24}..."

if [[ "$RESTORED_ID" == "$BEFORE_ID" ]]; then
  pass "burn_in_id가 원래 값으로 복귀 — rollback 검증"
else
  fail "rollback 후 burn_in_id가 원래와 다름"
fi

# Trap auto-cleanup
trap - EXIT

scenario_end
