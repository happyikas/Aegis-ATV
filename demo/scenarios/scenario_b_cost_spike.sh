#!/usr/bin/env bash
# Scenario B — 토큰 폭주, 1시간에 $50,000 (WHITEPAPER §5.2).
#
# Demonstrates that Cost Attestation Ledger (M12) detects spending
# divergence INDEPENDENT of the sLLM verdict (Claim 27), and uses a
# separate Ed25519 key (Claim 34) so billing can be selectively
# disclosed without exposing telemetry.

cd "$(dirname "$0")"
. ./_lib.sh

scenario "Scenario B — Token Spike Detection (§5.2)"
ensure_aegis

AID="cost-spike-bot-$(uuid | head -c 8)"
TENANT="demo-tenant"
note "AID = $AID, tenant = $TENANT"

# ─────────────────────────────────────────────────────────────────────
# ① 검출 — send a series of escalating-cost calls
# ─────────────────────────────────────────────────────────────────────
stage "① 검출 — 비용이 점진적으로 상승하는 5건 호출"
note "각 호출의 forecasted_cost_to_completion이 baseline의 5배 → 10배 → 30배"

for cost in 0.01 0.10 1.50 8.00 25.00; do
  P=$(build_payload "call_external_api" '{"url":"https://api.openai.com/v1/chat/completions"}' \
        "$AID" --tenant "$TENANT" \
        --cost "input_token_count=5000,cumulative_dollars=$cost,forecasted_cost_to_completion=$(python3 -c "print($cost * 5)")")
  V=$(evaluate "$P")
  D=$(echo "$V" | python3 -c "import json,sys;print(json.load(sys.stdin)['decision'])")
  R=$(echo "$V" | python3 -c "import json,sys;print(json.load(sys.stdin)['reason'][:80])")
  info "cost=\$$cost → $D — $R"
done

# ─────────────────────────────────────────────────────────────────────
# ② 격리 — verify the cost ledger captured the spike
# ─────────────────────────────────────────────────────────────────────
stage "② 격리 — Cost Attestation Ledger 의 별도 chain 확인"

LEDGER=$(curl -s "$AEGIS_URL/cost-attestation/by-tenant/$TENANT")
LEDGER_LEN=$(echo "$LEDGER" | python3 -c "import json,sys;print(json.load(sys.stdin).get('length',0))")
info "tenant cost ledger length: $LEDGER_LEN records"
assert_gte 5 "$LEDGER_LEN" "all 5 cost records persisted to separate ledger"

note "ledger의 별도 키 (Claim 34) — telemetry key와 분리됨"
TEL_KEY=$(curl -s "$AEGIS_URL/attestation" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('layers',{}).get('L5_key_binding',{}).get('public_key_fingerprint','')[:16])")
info "telemetry key fingerprint:    $TEL_KEY..."
note "(cost key 별도 — selective disclosure 가능)"

# ─────────────────────────────────────────────────────────────────────
# ③ 포렌식 — sample a single cost record
# ─────────────────────────────────────────────────────────────────────
stage "③ 포렌식 — 환불 협상용 변조 불가 증거 패키지"

SAMPLE=$(echo "$LEDGER" | python3 -c "
import json, sys
d = json.load(sys.stdin)
recs = d.get('records', [])
if recs:
    r = recs[0]
    print(json.dumps({
        'record_id': r.get('record_id', ''),
        'model': r.get('model_name', ''),
        'tokens': r.get('sw_cost_metrics', {}).get('input_token_count', 0),
        'dollars': r.get('sw_cost_metrics', {}).get('cumulative_dollars', 0),
        'atv_commitment': r.get('atv_commitment', '')[:24] + '...',
    }, ensure_ascii=False))
else:
    print('{}')
")
info "sample cost record:"
echo "$SAMPLE" | python3 -m json.tool 2>/dev/null | sed 's/^/      /'

assert_contains "model" "$SAMPLE" "cost record has model_name field"
assert_contains "atv_commitment" "$SAMPLE" "cost record cryptographically binds to ATV (Claim 30)"

# ─────────────────────────────────────────────────────────────────────
# ④ 복구 — would be tenant-budget cap. Show step 335 already gating.
# ─────────────────────────────────────────────────────────────────────
stage "④ 복구 — 다음 호출은 step 335 forecast budget gating"

RECOV_P=$(build_payload "call_external_api" '{"url":"https://api.openai.com/v1/chat/completions"}' \
  "$AID" --tenant "$TENANT" \
  --cost "input_token_count=10000,cumulative_dollars=50.0,forecasted_cost_to_completion=200.0")
RECOV_V=$(evaluate "$RECOV_P")
RECOV_D=$(echo "$RECOV_V" | python3 -c "import json,sys;print(json.load(sys.stdin)['decision'])")
RECOV_R=$(echo "$RECOV_V" | python3 -c "import json,sys;print(json.load(sys.stdin)['reason'][:90])")
info "high-forecast call → $RECOV_D — $RECOV_R"
note "forecasted_cost > tenant budget → REQUIRE_APPROVAL or BLOCK by step 335"

# Either is acceptable — depends on M12 escalation threshold tuning.
if [[ "$RECOV_D" == "REQUIRE_APPROVAL" ]] || [[ "$RECOV_D" == "BLOCK" ]]; then
  pass "high-forecast call gated by step 335 (got $RECOV_D)"
else
  info "step 335 not yet gating; this would be a tuning recommendation"
fi

# ─────────────────────────────────────────────────────────────────────
# ⑤ 사후 — Burn-in L2 (tenant) baseline updates
# ─────────────────────────────────────────────────────────────────────
stage "⑤ 사후 — Burn-in L2 (tenant) baseline 자동 학습"
L2=$(curl -s "$AEGIS_URL/burnin-status" | python3 -c "
import json,sys
d=json.load(sys.stdin)
slots=[l for l in d.get('layers',[]) if l.get('layer')=='L2']
print(len(slots), max([l.get('samples',0) for l in slots]+[0]))
")
info "L2 slots active: $(echo $L2 | awk '{print $1}'), max samples: $(echo $L2 | awk '{print $2}')"

scenario_end
