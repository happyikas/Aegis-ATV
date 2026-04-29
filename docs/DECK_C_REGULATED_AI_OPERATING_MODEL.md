# Deck C — Regulated AI Operating Model

**대상 청중:** Chief AI Officer / 디지털혁신 임원 / 정부·금융·의료 IT 책임자
**미팅 길이:** 45 분
**핵심 메시지:** \"AegisData = AI 시스템이 *production 가기 전 → 운영 중 → 후속 audit* 까지 단일 운영 모델\"
**커버하는 9-가치:** #5 사전 훈련 (Burn-in), #8 Agent identity, #9 Compliance — Regulated industry 의 lifecycle

---

## 1. 슬라이드 구성 (20 슬라이드, 45 분)

### Slide 1 — Hook (2 분)

> **\"규제 환경에서 AI 시스템이 production 에 가기 전 무엇을 검증해야 하는가?
> 운영 중에는 어떻게 모니터하고, 사고 후 무엇을 보존해야 하는가?\"**
>
> EU AI Act Article 9 (risk management), Article 12 (record-keeping), Article 17 (quality
> management), Article 61 (post-market monitoring). 4 단계 lifecycle.

오늘날의 답:
- **Pre-prod**: 한 번 보안팀 review, 끝
- **Prod**: Datadog 으로 latency 모니터, 사건 발생 시 forensic
- **Post**: 분기별 SOC 2 audit 위해 100 시간 evidence 수집

→ **lifecycle 전체를 한 시스템으로 운영하는 vendor 가 없다.**

### Slide 2 — Pain points (3 분)

| Lifecycle 단계 | 현재 상태 | 영향 |
|---|---|---|
| Pre-production | ad-hoc security review | drift detection 후행 |
| Identity / authorization | 시스템마다 다름 | multi-agent confusion |
| Operational monitoring | latency only | semantic drift 무인지 |
| Incident response | manual recovery | hours of downtime |
| Post-market monitoring | logs in 5 places | audit prep 100h+ |
| Compliance reporting | annual | regulator 답변 2 주 |

### Slide 3 — Architectural insight (3 분)

> AegisData 는 AI agent lifecycle 의 **4 단계** 를 단일 audit chain 위에서 운영:

```
        Pre-prod              Production            Post-market
    ┌─────────────┐       ┌─────────────┐      ┌─────────────┐
    │ Burn-in     │       │ Firewall +  │      │ Compliance  │
    │ M11 5-layer │ ────► │ ATMU 2PC +  │ ───► │ evidence    │
    │ shadow →    │       │ patrol +    │      │ 4 framework │
    │ assisted →  │       │ identity    │      │ auto-gen    │
    │ production  │       │ verification │      │             │
    └─────────────┘       └─────────────┘      └─────────────┘

           모든 단계의 record 가 동일 audit chain 으로 흘러감
```

### Slide 4 — Pre-prod: Burn-in (M11) — 5-layer 감독 (5 분)

새 AI agent 가 production 가기 전:

```
       L1 Observation   L2 Shadow    L3 Assisted   L4 Production    L5 Federated
       ──────────────    ─────────    ───────────   ─────────────    ────────────
       firewall만       firewall +     human in     full autonomy    cross-tenant
       관찰 (decision   파레렐 sLLM    -loop                          validated
       무시)            judge        (REQUIRE_APPROV
                                    AL 절반)
```

- Layer 별 graduate 조건 (TPR/FPR/precision threshold)
- `/burnin/label` 로 ground truth feedback
- 모든 layer transition signed + audit chain 기록
- **EU AI Act Article 17(2) (quality management) 자동 충족**

### Slide 5 — Pre-prod: Instruction baseline (M_step309) (3 분)

새 agent 의 system prompt / CLAUDE.md / .mcp.json 의 baseline 등록:

```bash
aegis baseline init      # 초기 snapshot
aegis baseline status    # 변경 detect
aegis baseline reattest  # 변경 승인 후 재서명
```

→ \"누가 system prompt 를 몰래 변경했나\" structurally detect.

### Slide 6 — Production: Identity (Claim 56) (4 분)

\[Deck B 의 Slide 8 와 같음, 강조점 다름\]

규제 환경 강조:
- 모든 agent 가 W3C DID (`did:aegis:<tenant>:<aid>`)
- Capability claim 에 규제 매핑 (예: `\"hipaa_phi_read\"`)
- DelegationChain 으로 \"intern agent 가 attending physician agent 의 권한 escalation\" 구조적 차단

### Slide 7 — Production: ATMU 2PC + rollback (3 분)

\[Deck B 의 Slide 4-5 요약\]

규제 강조:
- HIPAA 위반 도구 호출 시 즉시 rollback
- 송금 시스템: pre-recorded compensation plan
- 의료 결정: REQUIRE_APPROVAL → physician sign-off

### Slide 8 — Production: AuditPatrol (Claim 54) (3 분)

\[Deck A 의 Slide 6 요약\]

규제 강조:
- HIPAA 164.312(c)(1) **integrity** 요구사항: bit-rot 자동 감지
- EU AI Act Article 12(2)(c) **monitoring of operation** 자동 충족
- 24h 내 silent corruption 감지

### Slide 9 — Production: HW/SW double-check (Claim 26/27) (3 분)

\[Deck B 의 Slide 7 요약\]

규제 강조:
- Confidential AI 환경 (NVIDIA H100 CC, Apple PCC, Azure Confidential AI)
- compromised host 가 모델 swap 시 catch
- 법인 카드 fraud 감지와 비슷한 기제 (claimed cost vs measured)

### Slide 10 — Post-market: Compliance evidence (Claim 57) (5 분)

\[Deck A 의 Slide 8 요약, Regulated 강조\]

```bash
# EU AI Act Annex IV evidence 자동 생성 (분기):
curl -X POST /compliance/evidence -d '{
  \"framework\": \"eu_ai_act\",
  \"period_start_ns\": <Q1 시작>,
  \"period_end_ns\": <Q1 종료>,
  \"format\": \"markdown\"
}' > eu_ai_act_2026_Q1.md
```

| Framework | Coverage | 규제 영향 |
|---|---|---|
| **EU AI Act Annex IV** | 8/9 | 2026-08 high-risk system 시행 |
| **HIPAA 45 CFR § 164.312** | 6/7 | US health data 의무 |
| **SOC 2 TSC** | 9/9 | enterprise 영업 lock |
| **ISO/IEC 42001** | 6/6 | global AI management 표준 |

### Slide 11 — Post-market: Sovereignty (3 분)

\"우리 데이터가 미국 cloud 에 가는 게 부담된다\" — 정부 / 방위 / 금융 / 의료 sovereign 시장.

- AegisData 는 **on-prem first**: sidecar 로 배포, cloud 의존성 0
- Air-gap 환경 동작: dummy embedding + dummy judge mode
- 한국 KISA / KIRI 표준 호환 (post-quantum ML-DSA roadmap M18)
- 유럽 Gaia-X data sovereignty 호환
- 미국 FedRAMP / IL-5+ DoD roadmap

### Slide 12 — Lifecycle 통합 다이어그램 (3 분)

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                  Single Audit Chain (Ed25519 + Merkle)                │
   ├──────────────────────────────────────────────────────────────────────┤
   │                                                                       │
   │   Pre-prod                  Production                Post-market    │
   │   ────────                  ──────────                ───────────    │
   │                                                                       │
   │   Burn-in M11           Identity step308            Compliance        │
   │   layer transitions     verification                evidence           │
   │       │                     │                       generation         │
   │       │                     │                          │              │
   │   Instruction           Firewall (13 step)         Audit Patrol       │
   │   baseline drift            │                       (5 cadence)        │
   │       │                     │                          │              │
   │   policy review         ATMU 2PC                   Patrol report      │
   │       │                     │                          │              │
   │   approval signed       sign + chain               4-framework         │
   │                             │                       매핑                │
   │                         Cost ledger                                    │
   │                         (Claim 34)                                     │
   │                                                                       │
   └──────────────────────────────────────────────────────────────────────┘

      모든 record 가 동일 ATV-2080 텐서 + 동일 cryptographic primitive.
```

### Slide 13 — 비교: ad-hoc vs AegisData (3 분)

| Lifecycle 단계 | ad-hoc | AegisData |
|---|---|---|
| **Pre-prod review** | 보안팀 1 회 manual | M11 burn-in 5-layer × 4-phase |
| **Identity** | 시스템마다 다름 | W3C DID + capability claim |
| **Tool authorization** | role-based ad-hoc | step308 + delegation chain |
| **Operational monitoring** | Datadog logs | 13-step firewall + ATMU 2PC |
| **Incident response** | manual ticket | rollback strategy 자동 |
| **HW model swap detect** | unknown | Claim 26/27 |
| **Silent corruption** | months later | AuditPatrol 24h |
| **Compliance evidence** | annual 100h | Claim 57 분기 1h |
| **Post-market monitoring** | logs in 5 places | single audit chain |

### Slide 14 — Vertical 사례 (3 분)

#### 14.1 의료 (HIPAA + ISO 42001)
- Clinical decision support agent
- ePHI 접근에 capability claim `\"phi_read\"` 필수
- 모든 결정 audit chain 에 기록 (HIPAA 164.312(b))
- 분기별 OCR audit 시 `/compliance/evidence?framework=hipaa` 한 번

#### 14.2 금융 (SOC 2 + EU AI Act if EU operations)
- 회계 / 투자 / 송금 agent
- HW/SW double-check 가 model swap fraud 감지
- Cost attestation 별도 키 (Claim 34) → 회계 감사인 prompt 안 봄
- ATMU compensation plan 으로 잘못된 송금 자동 reverse

#### 14.3 정부 / 공공 (sovereign + EU AI Act)
- 시민 서비스 agent (민원, 복지, 입국심사)
- 모든 결정 forensic-grade audit (Ed25519 + Merkle + AES-GCM)
- 시민이 \"왜 거절됐는지\" 30-key attribution 으로 답 가능 (M13 unified head)
- 한국 KISA Post-Quantum 호환 (ML-DSA roadmap)

### Slide 15 — EU AI Act 시행 일정 (2 분)

```
   2024-08 : 발효
   2025-02 : prohibited AI 시행 (real-time biometric 등)
   2025-08 : GPAI provider 의무
   2026-08 : *high-risk AI system* 의무 시행 ← 우리 customer 의 D-day
   2026-08 : Annex IV record-keeping 의무
   2027-02 : 시스템 conformity assessment 의무
```

→ **2026-08 까지 12 개월**. enterprise 가 \"compliance-ready vendor\" 를 찾는 시기.

### Slide 16 — 우리 patent portfolio (2 분)

```
   US Provisional ATV_v7_10 (40 claims, 2024-Q4 출원)
        │
        ├─ Claim 1-40 : ATV-2080 schema, M13 head, 7-step firewall, ATMU 2PC,
        │              HW/SW double-check, cost attestation 별도 키, Burn-in 5-layer
        │
        ├─ Supplement v3 (17 claims, 2026 출원 예정)
        │   ├─ Claim 41-46 : performance advisory surface (KV cache / scheduling /
        │   │                placement / context window / unified head / advisor-as-hint)
        │   ├─ Claim 47    : cross-tenant federation (예약)
        │   ├─ Claim 48    : context window advisor
        │   ├─ Claim 49-50 : ATV diff compression / unified head v2 (예약)
        │   ├─ Claim 51-53 : production durability (group-commit / tiered archive / perf snapshot)
        │   ├─ Claim 54    : AuditPatrol periodic integrity check
        │   ├─ Claim 55    : multi-source HW telemetry aggregator
        │   ├─ Claim 56    : Agent identity + MCP + W3C DID + delegation chain
        │   └─ Claim 57    : Compliance evidence automation
```

→ **57 claims** 가 lifecycle 전체 cover.

### Slide 17 — Customer ROI (3 분)

가상 case (regulated SaaS, 1000 agents, EU operations):

| 단계 | Before | After |
|---|---:|---:|
| Pre-prod review | 2 weeks per agent | M11 burn-in 자동 (수일) |
| Identity audit | 시스템마다 다름 | W3C DID 표준 |
| Operational incident | 4-8h MTTR | <5 min ATMU rollback |
| HW fraud detect | unknown | 100% Claim 26/27 |
| EU AI Act compliance prep | 6 months, $500K | 1 month, <$50K |
| Compliance evidence gen | 100h × 4 = 400h/yr | 4h/yr |
| Sovereignty | cloud lock-in | on-prem ready |

**연간 ROI:** $1M+ saved + EU AI Act fine 회피 ($35M 잠재)

### Slide 18 — 다음 step (2 분)

3 단계:

1. **30-day technical pilot** (1 cluster, 1 tenant)
2. **Compliance gap mapping workshop** (기존 controls vs 우리 31 controls)
3. **Lifecycle integration roadmap** (burn-in → production → audit 순)

Pilot 비용: **무료**. 우리는 case study 와 patent licensable evidence 만 받음.

### Slide 19 — Q&A 예상 (3 분)

> **Q: 한 시스템에 너무 많은 책임 안 부여하나?**
> A: 모든 record 가 동일 audit chain 으로 흐른다는 게 *목적*. 5 vendor 의 5 시스템 보다 1 vendor 의 lifecycle 통합이 audit 효율 높음.

> **Q: vendor lock-in?**
> A: 모든 audit format 이 open: Ed25519 standard, JSONL, SQLite. ``aegis verify-audit`` 외부 도구로도 검증 가능. Patent license 협상 가능.

> **Q: 한국 정부 채택 가능?**
> A: K-PIPA + KISA Post-Quantum 호환 가능 (ML-DSA roadmap M18). 한글 manual 1070 줄 + Korean WHITEPAPER + PATENT_SUPPLEMENT 모두 준비됨.

> **Q: 우리 이미 vendor 5 개 쓰고 있는데?**
> A: Aegis 는 5 개 *위에* 깔리는 audit / compliance layer. Lakera 는 prompt 검출, Helicone 은 cost log, Datadog 은 latency. 우리는 그 위 sign + chain + compliance evidence.

> **Q: 우리 internal AI infra 가 이미 잘 됨**
> A: 그러시면 patent licensing 으로 충분. Source code 공개 + license + 한국 customer support.

### Slide 20 — Recap (2 분)

3 단계 takeaway:

1. **Pre-prod** = M11 5-layer Burn-in. Production 가기 전 정량 검증.
2. **Production** = 13-step firewall + ATMU 2PC + identity + HW/SW double-check.
3. **Post-market** = AuditPatrol + 4-framework Compliance evidence auto-gen.

**같은 audit chain. 같은 cryptographic primitive. lifecycle 전체.**

---

## 2. 부록 — 영업 자료

### 2.1 1-Page Executive Summary

```
AegisData = Single Operating Model for Regulated AI

Problem
  Regulated industries (healthcare, finance, government) need
  AI agent lifecycle covered: pre-prod validation, runtime safety,
  post-market evidence. Today's stack uses 5+ vendors.

Solution
  Single audit chain (Ed25519 + Merkle) covering 4 lifecycle stages:
    1. Burn-in: M11 5-layer × 4-phase pre-prod validation
    2. Production: 13-step firewall + ATMU 2PC + identity verify
    3. Monitoring: AuditPatrol 24h silent corruption detect
    4. Evidence: Compliance auto-gen for SOC2/EU AI Act/HIPAA/ISO 42001

Differentiators
  ① Lifecycle-spanning audit chain (no vendor sprawl)
  ② Patent portfolio: 57 claims across 17 supplements
  ③ Sovereignty-first: on-prem, air-gap capable
  ④ EU AI Act 2026-08 ready

Vertical fit
  Healthcare (HIPAA + ISO 42001)
  Finance (SOC 2 + EU AI Act)
  Government / Public sector (sovereign + EU AI Act)
  Defense (sovereign + ML-DSA post-quantum roadmap)

Patent
  US Provisional ATV_v7_10 + 17 supplements (Claims 1-57)
```

### 2.2 EU AI Act 매핑 워크시트

```
| Article | Aegis Coverage | 우리 evidence query type |
|---------|----------------|------------------------|
| Art. 9  | Risk management| burn-in TPR/FPR metric  |
| Art. 12 | Record-keeping | audit_chain (every call)|
| Art. 14 | Human oversight| step330_human + REQUIRE_APPROVAL |
| Art. 15 | Accuracy       | M11 burn-in metric       |
| Art. 17 | QMS            | burn-in 5-layer history  |
| Art. 61 | Post-market    | patrol_report + compliance|
| Annex IV| Documentation  | compliance/evidence?fw=eu_ai_act |
```

### 2.3 한국 정부 / 공공 영업 메시지

\"AegisData = AI 에이전트의 KISA 인증.\"

- K-PIPA (개인정보보호법) 호환: cost ledger 별도 키 = 회계만 보고 prompt 못 봄
- KISA Post-Quantum 로드맵 (ML-DSA M18 마일스톤)
- 한글 manual 1070 줄 + Korean PATENT_SUPPLEMENT 등 한국어 자료 준비
- 한국 customer 우선 partnership 가능

### 2.4 경쟁사 대응

| 경쟁사 | 우리 답변 |
|---|---|
| \"OpenAI / Anthropic 자체로 충분\" | \"제공자가 본인 모델의 audit 를 만든다 = conflict of interest. 독립 sidecar 가 표준.\" |
| \"AWS / Azure 의 confidential AI\" | \"hardware 만 보호. agent lifecycle audit 는 안 함.\" |
| \"OpenTelemetry GenAI 로 충분\" | \"OTel 은 telemetry 표준. 우리는 *signed* audit chain. layer 다름.\" |
| \"우리 internal infra 로 함\" | \"40-claim patent + 한글 자료 + on-prem-ready. 만드는 비용보다 license 효율적.\" |
