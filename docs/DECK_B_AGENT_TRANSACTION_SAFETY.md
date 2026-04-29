# Deck B — Agent Transaction Safety

**대상 청중:** Platform engineering / SRE / Site Reliability lead / Engineering VP
**미팅 길이:** 30 분
**핵심 메시지:** \"AegisData = AI 에이전트의 PostgreSQL\" — 모든 도구 호출이 ACID transaction 으로 보호됨
**커버하는 9-가치:** #3 SW/HW double-check, #4 오류 복구

---

## 1. 슬라이드 구성 (15 슬라이드, 30 분)

### Slide 1 — Hook (1 분)

> **\"agent 가 production database 에 \\`rm -rf\\` 를 보냈습니다. 무슨 일이 일어났나요?\"**
>
> 답:
> - LangChain: 명령 실행됨, 끝.
> - AutoGen: 명령 실행됨, callback 실행됨, 데이터 사라짐.
> - CrewAI: 같음.
> - **AegisData**: tentative → blocked at step320, intent_log 가 자동 rollback strategy 등록.

### Slide 2 — Pain points (3 분)

| 영역 | 현재 상태 | 영향 |
|---|---|---|
| Agent 의 destructive action | best-effort guardrails | data loss event |
| 부분 실행 | 어디까지 갔는지 audit 없음 | recovery 불가능 |
| Compromised host 감지 | 일반 IDS, AI-aware 아님 | model swap / billing fraud 무인지 |
| Compensation | 수동 ticket | response time hours |
| Multi-agent 부분 실패 | 추적 불가 | cascade failure |

### Slide 3 — 우리의 architectural insight (3 분)

> 데이터베이스가 **2-Phase Commit** 으로 transactional safety 를 보장하듯,
> AegisData 는 **agent 도구 호출** 을 2PC 로 감싼다.

```
       agent intent           Aegis ATMU                   external tool
       ─────────────          ──────────                   ─────────────
            │                     │                              │
            │ propose ────────►   │ tentative                    │
            │                     │                              │
            │                  firewall                          │
            │                  step308-340                       │
            │                     │                              │
            │                     │ prepared ────► (allowed) ──► │
            │                     │ aborted     ────────────────►│ (compensation)
            │                     │ committed   ◄── result ─────│
            │                     │                              │
            │ ◄── verdict ─────  │                              │
```

7 state machine: tentative → prepared → committed | aborted | rolled-back | compensated

### Slide 4 — Patent Claim 2/15 — ATMU 2PC (3 분)

\[ATMU = Agent Telemetry Management Unit\]

7-state machine:
- `tentative` — intent 기록, 아직 도구 호출 안 함
- `prepared` — firewall 통과, 호출 가능 표시
- `committed` — 실제 호출 완료, 결과 audit chain 에 기록
- `aborted` — pre-execution 차단 (side effect 0)
- `rolled-back` — checkpoint 복원 (file/git/process kill)
- `compensated` — irreversible 도구 (이메일 발송) 의 보상 액션 발행
- `failed` — terminal error

**핵심:** 사이드카가 **호출 이전** 에 compensation plan 을 미리 기록. 사후 보상이 enforce-able.

### Slide 5 — Rollback strategies — 4 종 (3 분)

| Strategy | 적용 도구 | 동작 |
|---|---|---|
| **file** | filesystem write/delete | tar snapshot 으로 복원 |
| **shell** | execute_shell, subprocess | child process kill + tree clean |
| **git** | code modification | git revert |
| **mcp** | MCP server tool | tool-specific compensation API |

```bash
aegis rollback --invocation-id <uuid>
# → file strategy 자동 선택, snapshot 복원
```

### Slide 6 — Compensation plan 예시 (3 분)

\"이메일 발송\" 같은 irreversible 도구는 rollback 불가. 대신 **pre-recorded compensation plan**:

```python
# 호출 *전* 에 미리 등록:
compensation_plan = {
    \"strategy\": \"send_followup_email\",
    \"params\": {
        \"original_message_id\": \"<uuid>\",
        \"correction_template\": \"correction_email_template.txt\",
    },
}
```

→ 호출 후 문제 발견 시 ATMU 가 자동 dispatch. SAP / Oracle / 송금 시스템과 같은 패턴.

### Slide 7 — Patent Claim 26/27 — HW/SW double-check (4 분)

\"compromised host 가 LLM 비용을 위조하면 어떻게 잡나요?\"

```
       SW band (1880-D)              HW band (200-D)
       ─────────────────             ─────────────────
       agent 자기 보고                 PMU + EDAC + NVML 측정
       \"100 tokens 썼어요\"          \"실제 5,000 FLOPS, 50× 차이\"

                  ↓                            ↓
                divergence math (M12)
                  ↓
                step337 HW anomaly gate
                  ↓
       BLOCK / REQUIRE_APPROVAL
```

[v4.1 collector framework 측정값]
- 8 source aggregator: PMU / EDAC / IOMMU / ethtool / NVML / BMC / TEE mock / FPGA mock
- T2 환경 ~70 % 실 데이터; T3 시 mock → real silicon swap

→ \"agent 가 자기보고 비용 100 tokens, HW 측정 5000 → 50× 위조\" 즉시 잡음.

### Slide 8 — Agent identity (Claim 56) — 권한 escalation 차단 (3 분)

\"누가 누구의 권한으로 무엇을 했나?\"

```python
agent_A (cap: read, write, shell)
    └── agent_B (cap: read, write)         # subset of A
            └── agent_C (cap: read)         # subset of B

    # B 가 \"shell\" 추가 시도 → BLOCK at step308
    # C 가 \"write\" 추가 시도 → BLOCK at step308
```

DelegationChain enforces:
- cap(child) ⊆ cap(parent) — capability escalation 구조적으로 불가능
- tenant_id 동일성
- parent_aid linkage 검증

→ multi-agent system 의 **structural safety**.

### Slide 9 — 7 stage Action Firewall (3 분)

```
agent tool call
    │
    ▼
step305 safe-allowlist        ← 안전 도구는 빠르게 ALLOW
step308 identity              ← Claim 56: 신원 + capability 검증
step309 instruction drift     ← prompt 변조 감지
step310 args                  ← 인자 위험 패턴
step311 donor rules           ← 9 종 룰팩
step312 normalize             ← 정규화
step315 AID auth              ← M14 per-aid circuit breaker
step320 blast                 ← 파괴력 평가
step330 human                 ← 운영자 in-the-loop
step335 cost                  ← 비용 발산 escalation
step336 loop                  ← redundant call 감지
step337 HW anomaly            ← Claim 26/27 SW/HW 불일치
step340 policy                ← sLLM judge final
    │
    ▼
verdict: ALLOW | BLOCK | REQUIRE_APPROVAL
    │
    ▼
ATMU intent_log: prepared → committed
    │
    ▼
audit chain: Ed25519 sign + Merkle link
```

→ 13 단계 검증을 모두 통과해야 도구 호출 release.

### Slide 10 — 비교 (2 분)

| | LangChain | AutoGen | CrewAI | **AegisData** |
|---|---|---|---|---|
| Tool call audit | callback log | logging | callback | **Ed25519 + Merkle chain** |
| Pre-execution block | guardrails (lib) | callback | guardrails | **7-stage firewall + verdict** |
| Rollback | ❌ | ❌ | ❌ | **4 strategies (Claim 2/15)** |
| Compensation plan | ❌ | ❌ | ❌ | **pre-recorded (Claim 2/15)** |
| HW/SW double-check | ❌ | ❌ | ❌ | **✅ Claim 26/27** |
| Identity escalation 차단 | ❌ | ❌ | ❌ | **✅ Claim 56** |
| Multi-agent delegation | partial | partial | partial | **DelegationChain Claim 56** |

### Slide 11 — Production deployment 패턴 (3 분)

```
   ┌─────────────────────────────────────────────────┐
   │              Kubernetes namespace                │
   │                                                  │
   │   ┌──────────┐         ┌─────────────────┐     │
   │   │  agent   │ ──HTTP─►│ Aegis sidecar   │     │
   │   │  pod     │         │ - firewall      │     │
   │   │          │         │ - ATMU          │     │
   │   │          │         │ - audit chain   │     │
   │   │          │         │ - patrol        │     │
   │   └──────────┘         └────────┬────────┘     │
   │                                 │              │
   │                                 ▼              │
   │              ┌──────────────────────────┐      │
   │              │  external tool / DB / API│      │
   │              └──────────────────────────┘      │
   │                                                  │
   └─────────────────────────────────────────────────┘
```

- Sidecar 배포 (latency: ~10 ms p99)
- 동일 process 배포 (latency: <1 ms)
- MCP middleware 배포 (Claim 56 — Anthropic MCP server-side hook)

### Slide 12 — Real scenario walkthrough (3 분)

**Scenario:** 회계 agent 가 *production* SQL 에 ``DROP TABLE invoices`` 시도.

```
1. agent 가 execute_sql tool 호출 (proposed)
2. Aegis sidecar receives /evaluate
3. step305: \"DROP TABLE\" 은 safe-allowlist 에 없음 → 통과 안 함
4. step308: identity proof 검증, capability=\"sql_read\" → DROP 권한 없음
   → BLOCK
5. ATMU intent_log: tentative → aborted
6. audit chain: Ed25519 sign + Merkle link
7. agent 응답: BLOCK reason=\"capability mismatch\"
```

총 latency: ~5 ms. **Database 손상 0**.

### Slide 13 — ROI (2 분)

가상 case (SaaS Fortune 500, 20 agent, 도구 호출/일 50 K):

| 항목 | Before | After |
|---|---:|---:|
| Production data loss event | 1-2 / year ($500K each) | 0 (avoided) |
| Recovery time per incident | 4-8 hours | <5 min |
| Compromised model swap detect | unknown | 100 % HW/SW double-check |
| Multi-agent capability bug | weeks | structural impossibility |

**연간 ROI:** $1M+ data loss 방지 + 운영 인력 절감.

### Slide 14 — Q&A 예상 (2 분)

> **Q: NeMo Guardrails 와 비교?**
> A: NeMo 는 *content* guardrails (toxicity, bias). 우리는 *transaction* safety. 같이 deploy.

> **Q: latency 가 부담?**
> A: p99 ~10 ms (sidecar). 도구 자체 latency 가 보통 50-500 ms 이라 <2 % 오버헤드.

> **Q: 우리 도구는 LangChain 기반인데?**
> A: LangChain callback 에 우리 사이드카 plug-in 1 줄. 모든 tool call 이 통과.

> **Q: HW counter 검증은 진짜 silicon 필요?**
> A: 현재 PMU/EDAC/IOMMU/NVML 5 source 가 실 데이터. TEE quote + Aegis-FPGA 만 mock. 비용 위조 / model swap 이미 catch 가능.

> **Q: ATMU compensation plan 의 execute 는 누가?**
> A: 우리 사이드카가 **dispatch** 하지 직접 *execute* 안 함. plan 의 \"strategy\" 가 외부 시스템 호출 (예: 송금 reverse API).

### Slide 15 — Next step (1 분)

\"production agent fleet 에 sidecar 1 개 deploy 후 1 주 보세요.\"
- ATMU intent_log 에 모든 도구 호출 자동 기록
- BLOCK / REQUIRE_APPROVAL 비율 측정
- 1 주 후: rollback 시뮬레이션 1 회 (file strategy)
- Pilot 비용: 무료

---

## 2. 부록 — 영업 자료

### 2.1 1-Page Executive Summary

```
AegisData = PostgreSQL of AI agents

Problem
  Multi-agent systems do best-effort tool calls. Production
  data loss is one rm -rf away. No structured rollback.

Solution
  ATMU 2-Phase Commit (Claim 2/15) wraps every tool call.
  Pre-recorded compensation plans + 4 rollback strategies.

Differentiators
  ① ATMU 7-state machine (tentative→prepared→committed→...)
  ② HW/SW double-check (Claim 26/27) — model swap 감지
  ③ DelegationChain capability subset enforcement (Claim 56)
  ④ 13-stage firewall before any side-effect

Use cases
  • Fintech multi-agent trading
  • Healthcare clinical decision support
  • Code-modifying agents (capability subset critical)
  • Cost-bound research agents (HW/SW divergence catches model swap)

Patent
  US Provisional ATV_v7_10 + Claims 2/15/26/27/56
```

### 2.2 Demo script

```bash
# 1. boot sidecar:
docker compose up -d

# 2. agent 가 dangerous SQL 시도 → ATMU 가 catch:
curl /evaluate -d '{
  \"header\": {...},
  \"tool_name\": \"execute_sql\",
  \"tool_args_json\": \"{\\\"query\\\":\\\"DROP TABLE users\\\"}\"
}'
# → {\"decision\":\"BLOCK\", \"reason\":\"step308 capability mismatch\"}

# 3. agent identity proof 갱신 (cap=\"sql_admin\" 부여):
curl /admin/aid/<aid> -d '{\"capabilities\": [\"sql_admin\"]}'

# 4. 다시 시도:
curl /evaluate -d '...'
# → {\"decision\":\"REQUIRE_APPROVAL\", \"reason\":\"step320 high-blast\"}

# 5. 운영자 승인 → committed → audit chain 기록.

# 6. rollback simulation:
aegis rollback --session-id <session>
# → file strategy 가 tar snapshot 복원.
```

### 2.3 경쟁사 대응

| 경쟁사 | 우리 답변 |
|---|---|
| LangChain callback 으로 충분 | \"callback 은 logging. 우리는 *transaction* — pre-execution BLOCK + rollback.\" |
| NeMo Guardrails | \"NeMo 는 content. 우리는 action transaction. 같이 deploy.\" |
| OPA / Open Policy Agent | \"OPA 는 policy 평가. 우리는 audit chain + rollback + HW/SW check. layer 다름.\" |
| Anthropic MCP 자체 | \"MCP 는 protocol. 우리는 그 protocol 위의 firewall + audit.\" |
| 자체 구현 | \"40-claim US patent + 17 supplements. NIH 보다 license 효율적.\" |
