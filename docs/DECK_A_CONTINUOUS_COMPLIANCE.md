# Deck A — Continuous Compliance & Cost Attestation

**대상 청중:** CISO / CFO / Compliance officer / 회계 감사인
**미팅 길이:** 30 분
**핵심 메시지:** \"AegisData = AI 시대의 SOX-grade audit infrastructure\"
**커버하는 9-가치:** #1 비용 절감, #2 성능 개선, #6 변조 방지, #7 헬스 체크, #9 Compliance

---

## 1. 슬라이드 구성 (15 슬라이드, 30 분)

### Slide 1 — Hook (1 분)

> **\"당신 회사가 다음 분기 OpenAI 청구서로 얼마를 받게 될지 정확히 예측할 수 있나요?
> 그 청구서가 위조되지 않았다는 것을 회계 감사인에게 어떻게 증명할 건가요?\"**

오늘날 enterprise 의 AI 비용은:
- 월별 $0 → $200K 까지 변동
- 회계 감사인이 검증할 audit trail 없음
- AI 위탁 vendor 가 제출하는 청구서가 \"진짜\" 인지 검증 불가

### Slide 2 — Pain points (3 분)

| 영역 | 현재 상태 | 영향 |
|---|---|---|
| AI 비용 폭증 | surprise bill, 예산 초과 | CFO 답변 압박 |
| 비용 검증 | vendor 청구서 받아쓰기 | 회계 감사 risk |
| 무결성 검증 | 사후 verify-audit 만 가능 | silent corruption 무인지 |
| Compliance evidence | 수동 매핑, 분기마다 100h+ | 인력/시간 비용 |
| EU AI Act 시행 (2026-08) | 준비 안 됨 | $35M fine risk |

### Slide 3 — 우리의 architectural insight (3 분)

> 보안 검증을 위해 만든 ATV-2080 텐서가 **이미 회계 감사에 필요한 모든 데이터를 담고 있다**.
> 별도 \"compliance 시스템\" 을 만들 필요 없이, audit chain 위에 framework 매핑만 얹으면 된다.

\[ATV-2080 architectural diagram — 30 subfield, cost_efficiency_metrics 16 슬롯 강조\]

### Slide 4 — Cost Attestation (Claim 34) — 별도 키 격리 (4 분)

```
       회계 감사인               sidecar
       ─────────────             ───────
       cost_ledger.pem           ed25519.pem
            │                          │
            ▼                          ▼
       cost_attestation         audit chain
       (signed cost only)       (signed full ATV)

       감사인은 이걸만 볼 수 있음
       prompt 내용은 절대 못 봄
```

- 매 도구 호출 = (audit record signed by ed25519.pem) + (cost record signed by cost_ledger.pem)
- 감사인 = cost_ledger.pub 만 받음. ATV body 못 봄
- **GDPR Art. 25 (data minimization) + EU AI Act + SOX 동시 만족**

### Slide 5 — KV cache advisor → 비용 절감 (3 분)

[v3.7 context advisor 측정값]
- 12-turn 시뮬레이션, budget 2000 tokens → **67 % 토큰 절감**
- p99 latency: 0.087 ms (50-turn history)
- 같은 ATV 가 KV cache + scheduling + memory placement + context window 4 축 모두 driven

\"비용 절감\" + \"비용 attestation\" 묶어서:
- **단순 비용 절감** 은 OpenAI/Anthropic 자체 가능
- **증명 가능한 비용 절감 (감사인이 검증 가능)** 은 우리만 가능

### Slide 6 — AuditPatrol (Claim 54) — 헬스 체크 (3 분)

\"sidecar 가 audit log 를 만들어요\" + \"근데 그 log 가 silent corruption 으로 망가졌으면?\"

| Patrol scope | Cadence | 검증 |
|---|---|---|
| sequence | 5 min | ATMU intent_log gap detection |
| sample | 1 hour | random 1% Ed25519 + SHA3 |
| consistency | 1 hour | SQLite ↔ JSONL ↔ encrypted journal |
| full | 6 hours | 모든 aid chain (Merkle + Ed25519) |
| cold | 24 hours | tiered archive cold tier sample decrypt |

→ \"silent corruption 발생 시 24 시간 안에 알림\" — 보험사 협상 카드

### Slide 7 — Crypto signing 4-tier (Claim 34) (3 분)

```
./keys/
├── ed25519.pem         ← telemetry (audit + approval)
├── ed25519_cost.pem    ← Claim 34: 감사인 격리
├── journal_data.key    ← AES-GCM (M15)
└── ham_data.key        ← AES-GCM (M16)
```

- **매 record Ed25519 signed + Merkle-chained**
- AES-256-GCM AEAD encrypted journal (forensic-grade)
- 4-key separation 으로 권한 분리 (regulator / billing / forensic / memory)

### Slide 8 — Compliance automation (Claim 57) — 프레임워크 자동 매핑 (4 분)

```bash
# Q1 SOC 2 evidence packet 생성:
curl -X POST /compliance/evidence -d '{
  \"framework\": \"soc2\",
  \"period_start_ns\": 1735689600000000000,
  \"period_end_ns\": 1743465600000000000,
  \"format\": \"markdown\"
}' > soc2-2026-Q1.md
```

| Framework | 매핑 | 미매핑 |
|---|---:|---:|
| SOC 2 TSC | 9/9 | 0 |
| EU AI Act Annex IV | 8/9 | 1 (training=model provider) |
| HIPAA 164.312 | 6/7 | 1 (TLS=mesh) |
| ISO/IEC 42001 | 6/6 | 0 |

→ **분기별 100+ 시간** 의 수동 evidence 수집 → **API 한 번** 으로 압축

### Slide 9 — Continuous Compliance Loop 다이어그램 (2 분)

```
               agent 도구 호출
                     │
                     ▼
       ┌─────────────────────────┐
       │   Aegis sidecar         │
       │  - 7-step firewall      │
       │  - sign + chain         │
       │  - cost ledger          │
       │  - encrypted journal    │
       └────────┬────────────────┘
                │
       ┌────────┴────────┐
       │                 │
       ▼                 ▼
  AuditPatrol      Compliance API
  (백그라운드        (분기별
   무결성 검증)       evidence 생성)
       │                 │
       ▼                 ▼
   alert        SOC 2 evidence packet
                EU AI Act packet
                HIPAA packet
                ISO 42001 packet
```

### Slide 10 — 비교 (2 분)

| | Helicone / Portkey | Lakera | Datadog LLM | **AegisData** |
|---|---|---|---|---|
| 비용 logging | ✅ | ❌ | ✅ | ✅ |
| **비용 attestation (별도 키)** | ❌ | ❌ | ❌ | **✅ Claim 34** |
| Audit chain | basic | ❌ | basic | **Ed25519 + Merkle** |
| Background patrol | ❌ | ❌ | ❌ | **✅ Claim 54** |
| Compliance evidence auto | ❌ | ❌ | partial | **✅ 4 framework Claim 57** |
| EU AI Act ready | ❌ | ❌ | partial | **✅ Annex IV 8/9** |

### Slide 11 — Customer ROI (2 분)

가상 case (Fortune 500, 50 agent, 월 $50K AI 비용):

| 항목 | Before | After |
|---|---:|---:|
| 비용 surprise (월) | $50K → $200K spike | predictable, alert 5min |
| 토큰 절감 (context advisor) | — | 30 % = $15K/월 |
| Compliance evidence 시간 | 100h/분기 | <1h/분기 |
| Silent corruption 감지 | 0 (몰랐음) | 24h 내 |
| 회계 감사 cost ledger 분리 | 불가능 | Claim 34 표준 |

→ **연간 ROI: $300K+ saved + EU AI Act fine risk eliminated**

### Slide 12 — 다음 step (2 분)

- (a) **30-day pilot**: 우리 사이드카 1 개 cluster 에 deploy. 한 분기 evidence 자동 생성.
- (b) **Audit walkthrough**: 우리 audit chain 의 cryptographic 보장을 회계 감사인 / CISO 와 walkthrough.
- (c) **Compliance gap mapping**: 당신 회사 현재 SOC 2 controls 와 우리 31 controls 매핑 — gap 명시.

### Slide 13 — Q&A 예상 질문 (2 분)

> **Q: 우리 이미 Helicone 쓰고 있는데?**
> A: Helicone 은 cost logging, 우리는 cost **attestation** (별도 키 격리, Merkle chain). 회계 감사인용 evidence 는 Helicone 으로 안 됨.

> **Q: AuditPatrol 의 false positive 는?**
> A: 6 finding category 의 critical (signature/hash/chain/aead/seq_gap) 은 false positive 거의 없음 (cryptographic primitive 라). consistency 만 SQLite ↔ JSONL replication lag 으로 false positive 가능 — warning 으로만 표시.

> **Q: EU AI Act 시행 후 ANNEX_IV_2_b training procedure 가 필요한데?**
> A: 그건 **model provider** (OpenAI / Anthropic) 책임. 우리는 그 evidence 를 *수집* 하는 vendor 가 아니라 *agent 호출* evidence 를 모음. 두 vendor 가 같이 EU AI Act 만족시킴.

> **Q: 가격은?**
> A: enterprise license — annual subscription. Pilot 30 day 무료.

### Slide 14 — Recap (1 분)

3 가지 takeaway:

1. **Cost attestation = Claim 34 별도 키 격리.** 회계 감사인이 prompt 안 보고 비용만 검증.
2. **Continuous compliance = AuditPatrol + 4-framework 자동 매핑.** 분기별 100h → 1h.
3. **Cryptographic backbone = Ed25519 + Merkle + AES-GCM 4-tier.** SOX-grade audit infrastructure.

### Slide 15 — Next step (1 분)

\"30 일 pilot 시작합시다.\"
- 1 cluster, 1 tenant, sidecar 1 개
- 1 분기 후 evidence packet auditor 에 제출
- Pilot 비용: 무료. 우리는 case study 만 받음.

---

## 2. 부록 — 영업 자료

### 2.1 1-Page Executive Summary

```
AegisData = SOX-grade audit infrastructure for AI agents

Problem
  Enterprise AI bills are unpredictable, unverifiable, and
  uncompliant with EU AI Act / SOC 2 / HIPAA.

Solution
  Same audit chain that signs every agent tool call (Ed25519 +
  Merkle), with separate cost-attestation key (Claim 34) so the
  accountant verifies costs without seeing prompts.

Differentiators
  ① Cost attestation key ≠ telemetry key (Claim 34)
  ② Background AuditPatrol catches silent corruption (Claim 54)
  ③ Compliance evidence automation for 4 frameworks (Claim 57)
  ④ 29/31 controls mapped, 2 honestly flagged

Pricing
  Annual enterprise subscription. Pilot first 30 days free.

Patent
  US Provisional ATV_v7_10 + 17 supplements (Claims 41-57)
```

### 2.2 영업 미팅 체크리스트

- [ ] CFO + CISO + Compliance officer 동시 참석
- [ ] 다음 SOC 2 audit 일정 확인 (90 일 전 plug-in 가능)
- [ ] EU operations 유무 확인 (EU AI Act 적용 여부)
- [ ] 현재 LLM observability 도구 inventory (Helicone / Langfuse / Datadog)
- [ ] 데이터 sovereignty 요구사항 (on-prem / cloud / hybrid)
- [ ] 30-day pilot scope 합의

### 2.3 경쟁사 대응 (one-liner)

| 경쟁사 | 우리 답변 |
|---|---|
| \"Helicone/Portkey 쓰고 있어요\" | \"그건 cost *logging*. 우리는 cost *attestation* — 별도 키로 회계 감사인 격리.\" |
| \"Datadog LLM Observability 있어요\" | \"Datadog 은 *observability*. 우리는 *compliance evidence* — auto-generate SOC 2 packet.\" |
| \"NVIDIA Confidential Computing 쓰면 되잖아\" | \"NVIDIA CC 는 inference 만 보호. agent 비용 위조 / 도구 호출 audit 는 cover 안 함.\" |
| \"OpenAI moderation 으로 충분\" | \"moderation 은 prompt 검사. cost attestation + SOC 2 evidence 와 다른 layer.\" |
| \"Lakera 와 비교?\" | \"Lakera 는 prompt injection 방어 전문. 우리는 cost / compliance / audit chain. 같이 deploy.\" |
