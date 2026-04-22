#!/usr/bin/env bash
# Scenario F — 변조된 메모리로 잘못된 결정 (WHITEPAPER §5.6).

cd "$(dirname "$0")"
. ./_lib.sh
HAM() { python3 ./_ham.py "$@"; }

scenario "Scenario F — Tampered Memory Detection (§5.6)"
ensure_aegis

AID="finance-rag-bot-$(uuid | head -c 8)"
TENANT="demo-tenant"
note "AID = $AID — finance team RAG agent"

# ─────────────────────────────────────────────────────────────────────
# Stage 1 — store the original (correct) wiki entry
# ─────────────────────────────────────────────────────────────────────
stage "1단계 — 정상 entry (Q3 매출 정확값)"
CORRECT_BODY='{"kpi":"q3_revenue_usd","value":2400000,"source":"finance/q3_internal_v1"}'
CORRECT_ID=$(HAM memory "$AID" "$TENANT" "$CORRECT_BODY" "kpi,q3,revenue,verified" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['object_id'])")
info "정상 entry stored: $CORRECT_ID  (revenue = \$2,400,000)"

# ─────────────────────────────────────────────────────────────────────
# Stage 2 — tampered entry
# ─────────────────────────────────────────────────────────────────────
stage "2단계 — 누군가 wiki를 변조 (\$2.4M → \$24M, 10x 부풀림)"
TAMPERED_BODY='{"kpi":"q3_revenue_usd","value":24000000,"source":"finance/q3_wiki_revision_47"}'
TAMPERED_ID=$(HAM memory "$AID" "$TENANT" "$TAMPERED_BODY" "kpi,q3,revenue,wiki" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['object_id'])")
info "변조 entry stored:   $TAMPERED_ID  (revenue = \$24,000,000)"

# ─────────────────────────────────────────────────────────────────────
# Stage 3 — recall + ground
# ─────────────────────────────────────────────────────────────────────
stage "3단계 — agent가 두 entry retrieve 후 분기 보고서 초안 생성"
RECALL=$(HAM recall "$AID" "$TENANT" "q3,revenue")
RECALLED=$(echo "$RECALL" | python3 -c "import json,sys;print(json.load(sys.stdin)['length'])")
info "recall(tags=['q3','revenue']): $RECALLED items"

stage "  → ground: 보고서가 인용한 entry binding"
GROUND=$(HAM ground "$AID" "$TENANT" "Q3 revenue figure cited in the quarterly board report draft" "$CORRECT_ID,$TAMPERED_ID")
CLAIM_HASH=$(echo "$GROUND" | python3 -c "import json,sys;print(json.load(sys.stdin)['claim_hash'][:16])")
BOUND=$(echo "$GROUND" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['references']))")
info "claim_hash: $CLAIM_HASH..."
info "bound: $BOUND entries referenced"
assert_eq "2" "$BOUND" "both entries (correct + tampered) bound to report"

# ─────────────────────────────────────────────────────────────────────
# Stage 4 — detect inconsistency
# ─────────────────────────────────────────────────────────────────────
stage "4단계 — inconsistency 검출: 같은 kpi 두 값 충돌"
INCONSISTENCY=$(echo "$RECALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d.get('items', [])
revenues = {}
for it in items:
    body = it.get('body', {})
    if body.get('kpi') == 'q3_revenue_usd':
        revenues[it['object_id']] = body['value']
if len(set(revenues.values())) > 1:
    sorted_vals = sorted(revenues.values())
    ratio = max(revenues.values()) / min(revenues.values())
    print(f'INCONSISTENT: same kpi, {len(set(revenues.values()))} distinct values')
    print(f'  values: {sorted_vals}')
    print(f'  ratio:  {ratio:.1f}x')
else:
    print('CONSISTENT')
")
echo "$INCONSISTENCY" | sed 's/^/      /'

if echo "$INCONSISTENCY" | grep -q "INCONSISTENT"; then
  pass "inconsistency detected — production deployment auto-flags for review"
fi
note "T3 M25 (linkage_consistency_features 2060..2079) 가 자동화 — 현 T2 는 수동"

# ─────────────────────────────────────────────────────────────────────
# Stage 5 — forget tampered + verify clean
# ─────────────────────────────────────────────────────────────────────
stage "5단계 — 복구: 변조 entry tombstone"
FORGET=$(HAM forget "$AID" "$TENANT" "$TAMPERED_ID" "inconsistency with verified source")
OK=$(echo "$FORGET" | python3 -c "import json,sys;print(json.load(sys.stdin)['ok'])")
assert_eq "True" "$OK" "tampered entry tombstoned"

RECALL2=$(HAM recall "$AID" "$TENANT" "q3,revenue")
RECALLED2=$(echo "$RECALL2" | python3 -c "import json,sys;print(json.load(sys.stdin)['length'])")
info "recall after forget: $RECALLED2 items"
assert_eq "1" "$RECALLED2" "only correct entry surfaces now"

GROUND2=$(HAM ground "$AID" "$TENANT" "Q3 revenue figure (post-correction)" "$CORRECT_ID,$TAMPERED_ID")
B2=$(echo "$GROUND2" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['references']))")
M2=$(echo "$GROUND2" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['missing']))")
info "after forget — bound: $B2, missing: $M2"
assert_eq "1" "$B2" "1 reference still bound (correct entry)"
assert_eq "1" "$M2" "1 reference now missing (tombstoned tampered entry)"

scenario_end
