# Aegis × Hermes — 통합 분석

**작성**: 2026-05-14
**상태**: 분석 + `aegis label` (PR-this) 로 첫 코드 surface 연결
**대상**: Hermes Agent — "self-improving AI agent" (server-resident, skill 누적, 세션 횡단 user model 심화)

---

## 1. 한 줄 요약

> Hermes 는 **agent 자체가 자기 개선**하는 시스템. Aegis 는 그 agent **밖에서** 행동을 관찰 / 정책 검증 / cryptographic audit 하는 시스템. 두 시스템은 **다른 레이어**에 있고, Hermes 가 self-improving 일수록 Aegis 의 가치가 커집니다 — *self-improvement 의 안전성을 누가 cross-check 하나?*

---

## 2. 두 시스템의 레이어 매핑

```
┌─────────────────────────────────────────────────────────┐
│  Hermes Agent (self-improving)                          │
│    - skill bundle: 경험에서 만들어 누적                  │
│    - persistent memory: 세션 횡단 user model 심화        │
│    - server-resident: 오래 돌수록 능숙                   │
│    - 자기 개선 결정의 주체 = 자기 자신                   │
└────────────────────┬────────────────────────────────────┘
                     ▼  tool call
┌─────────────────────────────────────────────────────────┐
│  Aegis ATV (external observer / firewall / audit)       │
│    - 16-step Action Firewall                            │
│    - SHA3-chained Ed25519-signed audit log              │
│    - Coach burn-in: 4-phase × 5-layer baseline          │
│    - 8-advisor pipeline: cost/security/loop-breaker/…   │
│    - sLLM Judgment Engine: subfield_attribution         │
│    - 자기 개선 결정의 주체 = 운영자 (offline retrain)    │
└─────────────────────────────────────────────────────────┘
```

핵심: **Hermes 는 자기 코드/skill 을 갱신합니다. Aegis 는 그 갱신이 안전한 방향인지 외부에서 cross-check 합니다.**

---

## 3. Aegis 가 학습할 수 있는 5 패턴 — 코드 매핑 (정직하게)

사용자가 처음 제시한 5 패턴을 Aegis 가 "지금 학습할 수 있는 것 / 인프라만 있는 것 / 미구현" 으로 정직하게 분류:

| 패턴 | 구현 상태 | 코드 근거 |
|---|---|---|
| **(a) Hermes 가 정상적으로 일할 때의 tool pattern** | 🟢 학습됨 | `step305_safe_actions.py` allowlist 가 정상 패턴 fast-path. `burnin/anomaly.py` 의 L1-L5 baseline 이 μ/σ/p50/p95/p99 통계로 정상 분포 저장 (`~/.aegis/burnin_baseline.json`) |
| **(b) Role 별 정상 skill usage pattern** | 🟡 인프라만 | `burnin/controller.py:104` 의 `LayerKey("L4", tenant, role)` 가 role 별 baseline 키 분리. **하지만 "skill" 자체는 ATV-2080 30 subfield 의 1차 객체가 아님** — Hermes 의 skill ID 를 어디에 매핑할지는 v3 schema 결정 필요 |
| **(c) Memory update 가 나중에 실패를 유발하는 패턴** | 🔴 미구현 | "memory mutation event" 가 ATV subfield 에 없음. step309 의 instruction drift baseline 은 *외부 instruction 파일* (CLAUDE.md, .mcp.json) 만 감지. agent-internal memory 변경 → N step 뒤 실패 lag pattern 학습 surface 없음 |
| **(d) MCP/tool sequence → 비용 폭주 패턴** | 🟢 학습됨 | `judge/action_advice.py` 의 `cost-optimizer` advisor + `swap-model` verb. step336 loop detector 가 same-call ≥3x → REQUIRE_APPROVAL. `aegis cost summary` 가 sequence-level cost trajectory 추적 |
| **(e) User/project 별 허용 가능한 autonomy** | 🟡 stub | Burn-in 4-phase (OBSERVATION → SHADOW → ASSISTED → PRODUCTION) 가 autonomy 게이팅 인프라. **per-user 설정 스위치는 코드에 없음** — 현재는 글로벌 phase state |

→ Hermes 환경에서 **즉시 가치 (a, d)**, **튜닝 필요 (b, e)**, **새 surface 필요 (c)**.

---

## 4. sLLM Judgment Engine — Hermes 환경에서 실제 동작 흐름

매 tool call 마다:

```
Hermes 가 tool 호출 결정
        │
        ▼
Aegis hook (PreToolUse) 가 가로챔
        │
        ▼
ATV 구성: 30 subfield → 2080-D vector
        │
        ▼
step340 sLLM judge (AEGIS_JUDGE_PROVIDER 선택)
    │
    ├─ dummy:          regex 룰 (Solo Free 기본)
    ├─ attribution_head: M13 frozen linear, <1ms
    ├─ local-phi:      Phi-3-mini-q4, ~50ms
    ├─ haiku:          Claude Haiku 4.5, ~150ms + RAG corpus
    └─ hybrid:         confidence-routing combiner
        │
        ▼
JudgeVerdict {
    decision: ALLOW | REQUIRE_APPROVAL | BLOCK,
    confidence: [0..1],
    subfield_attribution: { 30개 subfield 각각의 기여도 }   ← Hermes 의 어느 행동 차원이 위험한지 노출
}
        │
        ▼ (parallel)
8-advisor pipeline → ActionAdvice {
    recommended_advisors: [
        cost-optimizer / kv-cache-optimizer / security-reviewer /
        context-compactor / test-runner / loop-breaker /
        permission-escalator / human-clarifier
    ],
    각각 action_steps[{verb, params, expected_impact, cited_signals}]
}
```

**Hermes 가 self-improve 할 때 Aegis 의 입력 신호가 변합니다**:
- 새 skill 도입 → tool sequence 분포 변화 → L5 baseline 의 anomaly z-score
- 새 memory write 패턴 → step309 가 (instruction baseline 이라면) detect / (agent memory 라면) 미감지
- 비용 trajectory 변화 → cost-optimizer advisor 가 "swap-model" 권고
- 새 skill 첫 사용 → step336 loop detector + REQUIRE_APPROVAL fallback

---

## 5. Burn-in 의 "지속 보정" — Hermes 와 결합 시 cadence

Patent 의 4-phase 사이클:

| 단계 | Hermes 환경에서 의미 | 전환 게이트 |
|---|---|---|
| **Observation** | Hermes 가 production 에서 일하는 동안 Aegis 는 passive 수집만 — verdict 영향 없음 | ≥1000 samples |
| **Shadow** | Aegis 가 verdict 를 *내지만 enforce 안 함* — shadow.jsonl 에 기록. Hermes 의 self-improvement 가 baseline 을 흔들면 이 단계에서 보임 | TPR ≥0.95, FPR ≤0.02, precision ≥0.90 |
| **Assisted** | Aegis verdict 가 human approval 로 escalate. Hermes 의 새 skill 도입 직후 권장 단계 | human override rate ≤5% |
| **Production** | Aegis 가 autonomously 차단 / 승인 | — |

**중요한 코드 reality**: 현재 Aegis 의 sLLM 가중치는 **frozen** (M13 attribution_head_v1.json) + baseline 통계는 **수동 retrain** (`aegis burnin retrain`). 즉 **live delta-training loop 없음**.

→ Hermes 가 self-improve 하는 cadence 와 Aegis 가 retrain 하는 cadence 의 **mismatch 가 핵심 운영 변수**:
- Hermes 가 빨리 변하면 Aegis baseline 의 drift z-score 가 false positive 폭증 → Assisted 단계에서 멈춰야 함
- Hermes 가 안정되면 Aegis baseline 도 안정 → Production 으로 승격

**권장 cadence**: Hermes 의 skill 업데이트 후 24h 이내 `aegis burnin retrain` + `aegis baseline reattest` 실행.

---

## 6. Labeling pipeline — patent 의 4 source vs 현재 코드

Patent ¶[0083] 은 sLLM training corpus 의 라벨 source 를 4 가지 명시:

| Source | 코드 상태 | 어떻게 채우나 |
|---|---|---|
| **1. Human analyst adjudication** | 🟢 **이번 PR 에서 신설** | `aegis label <selector> --label {benign\|suspicious\|malicious}` → `~/.aegis/labels.jsonl`. trainer 가 trace_id 로 shadow 와 join |
| **2. Post-hoc incident labeling** | 🟡 부분 | `aegis replay` + `aegis forensic last` 로 historical 조회 가능. **명시적 라벨 부여는 #1 의 `aegis label --invocation <id>` 로 처리** |
| **3. Red-team simulation** | 🟡 부분 | `aegis soak` 가 load harness. red-team-specific scenario 는 미구현 |
| **4. Counterfactual synthesis** | 🟡 부분 | `burnin/m13_data.py` 의 210-example synthetic corpus (6 카테고리: benign_read, destructive_bash, credential_leak, database_mutation, sensitive_path, cloud_destructive). 명시적 counterfactual generator 는 미구현 |

이번 PR 로 **#1 source 가 처음 코드에 실재**합니다. Hermes 의 self-improvement 가 의심스러운 케이스가 audit log 에 남으면, 운영자가:

```bash
# 가장 최근 PreToolUse 가 의심스러우면
aegis label --last --label suspicious --reason "skill X 새 도입 후 첫 db.write"

# 특정 invocation_id 를 사후 라벨링
aegis label --invocation inv-abc123 --label malicious \
    --reason "post-hoc: 이 호출이 결국 데이터 손실 원인" \
    --analyst alice@team

# 검토
aegis label list
aegis label show <trace_id>
```

다음 `aegis burnin train-m13` 실행 시 trainer 가 labels.jsonl 을 shadow.jsonl 과 trace_id 로 join → 인간 라벨이 ground truth → M13 weights 가 *실제 사람의 판단* 으로 갱신. self-labeled-only 가 아닌 진짜 supervised 학습.

---

## 7. 3-layer stack — 구체적 셋업

Hermes / OpenClaw / Claude Code 등 어느 agent runtime 이든 패턴 동일:

```bash
# 1) Aegis sidecar (Python, FastAPI) — tool call 가로채는 firewall + audit chain
docker compose up -d   # 또는: uv run uvicorn aegis.main:app

# 2) Hermes agent — 평소 운영대로. tool call 시 Aegis sidecar 가 PreToolUse 후크
#    (Hermes 가 hook 컨트랙트를 지원하면 직접 연결. 아니면 wrapper 경유)

# 3) Burn-in 활성화 — observation 단계로 시작
export AEGIS_BURNIN_SHADOW=1
# (Hermes 가 만든 tool call 마다 ~/.aegis/shadow.jsonl 누적)

# 4) 의심 case 가 보이면 사람이 라벨링
aegis label --last --label suspicious --reason "..."

# 5) 주기적 retrain
aegis burnin retrain
aegis baseline reattest
```

---

## 8. 정직한 scope — 미구현 명시

이 통합 분석이 *현재 코드로 가능한 것* 과 *patent claim 의 약속* 의 차이를 명시합니다:

| Patent 약속 | 코드 reality | 어떻게 메우나 |
|---|---|---|
| sLLM 이 "continuously self-calibrates" | 가중치 frozen, 수동 retrain | `aegis burnin train-m13` 정기 실행 cadence |
| 4-phase 자동 전환 | gate 메트릭 측정은 있으나 자동 전환 코드 없음 | 운영자가 `aegis burnin status` 보고 수동 승격 |
| Hermes-specific skill 추적 | ATV-2080 30 subfield 에 skill_id 없음 | v3 schema 에 `skill_id` 추가 (#147 다음 surface 후보) |
| Memory mutation → lag failure 학습 | 미구현 | v3 schema 에 `memory_mutation` event 추가 + shadow phase 에 (mutate at t0, fail at t1) 쌍 수집 |
| Live online learning loop | 미구현 (offline retrain only) | Hermes cadence 와 Aegis retrain cadence 의 mismatch 를 운영 변수로 인정 |

이 표가 *Hermes 사용자가 Aegis 를 도입했을 때 기대 vs 현실* 의 base contract 입니다.

---

## 9. Show HN 본문에 인용할 수 있는 한 줄

> "Hermes 가 자기 코드/skill 을 갱신할 때 그 갱신이 안전한 방향인지 누가 검증합니까? Aegis 는 그 외부 검증 layer 입니다. Hermes 의 self-improvement → tool call sequence → Aegis 의 cryptographic audit chain + 16-step firewall + 8-advisor pipeline."

---

## 10. 다음 surface 후보

1. **ATV v3 schema 에 `skill_id` + `memory_mutation` 추가** — (b), (c) 패턴 enable
2. **`aegis burnin status --auto-retrain-suggest`** — shadow 충분히 모이면 retrain 권장 알림
3. **`aegis labgen counterfactual`** — base ATV 를 받아 subfield 단위 perturbation → synthetic corpus 확장
4. **Hermes 전용 baseline preset** — `aegis burnin init --agent-class hermes`
5. **자동 phase 전환** — gate 메트릭 만족 시 자동 승격 (현재는 수동)

이 5개는 별도 issue 로 추적합니다 (이번 PR 의 범위는 #1 + #4).
