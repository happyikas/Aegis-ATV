# AegisData Enterprise Sales Deck Index

3 deck for 3 audiences. Each deck repackages the **9-value framework**
into a single coherent message. Pick the deck that matches your meeting.

---

## Deck A — [Continuous Compliance & Cost Attestation](DECK_A_CONTINUOUS_COMPLIANCE.md)

**Audience:** CISO / CFO / Compliance officer / 회계 감사인
**Length:** 30 min · 15 slides
**Tagline:** \"AI 시대의 SOX-grade audit infrastructure\"

**Covers 9-value items:**
- #1 Cost monitoring (cost attestation 별도 키)
- #2 Performance optimisation (KV cache → token savings)
- #6 Tamper resistance (Ed25519 + Merkle + AES-GCM)
- #7 Health check (AuditPatrol)
- #9 Compliance automation (4 framework)

**Key claim:** *증명 가능한* 비용 절감 + Continuous Compliance.

**Target close:** 30-day pilot + audit walkthrough.

---

## Deck B — [Agent Transaction Safety](DECK_B_AGENT_TRANSACTION_SAFETY.md)

**Audience:** Platform engineering / SRE / Engineering VP
**Length:** 30 min · 15 slides
**Tagline:** \"PostgreSQL of AI agents\" — every tool call wrapped in ACID transaction

**Covers 9-value items:**
- #3 Misbehaviour detection (HW/SW double-check)
- #4 Error recovery (ATMU 2PC + 4 rollback strategies)

**Key claim:** Multi-agent system 의 **structural safety** — capability escalation 구조적으로 불가능.

**Target close:** Production sidecar deployment + rollback drill.

---

## Deck C — [Regulated AI Operating Model](DECK_C_REGULATED_AI_OPERATING_MODEL.md)

**Audience:** Chief AI Officer / 디지털혁신 임원 / 정부·금융·의료 IT 책임자
**Length:** 45 min · 20 slides
**Tagline:** \"AI 시스템의 pre-prod → production → post-market 단일 운영 모델\"

**Covers 9-value items:**
- #5 Pre-training (Burn-in M11 5-layer × 4-phase)
- #8 Agent identity (W3C DID + delegation chain + capability escalation 차단)
- #9 Compliance (EU AI Act 2026-08 ready, K-PIPA, KISA)

**Plus integrative coverage of:**
- Lifecycle-spanning audit chain
- 57-claim patent portfolio
- Sovereignty / on-prem / air-gap

**Target close:** Lifecycle integration roadmap + EU AI Act readiness assessment.

---

## 어떤 deck 을 언제 쓰나

| 미팅 시나리오 | Deck |
|---|---|
| CFO/회계감사 \"AI 비용 검증\" | **A** |
| CISO + 보안 review | **A** |
| SRE / DevOps lead \"agent fleet 운영\" | **B** |
| Engineering VP \"production safety\" | **B** |
| 의료 / 금융 / 정부 / 공공 의사결정자 | **C** |
| EU operations 책임자 (AI Act 시행 대응) | **C** |
| Multi-vendor consolidation 검토 | **C** |
| 한국 정부 / KISA 관계자 | **C** (한국어 자료 강조) |

---

## 9-value framework cross-reference

각 9-value 가 어느 deck 에서 강조되는지:

| # | 9-Value | Deck A | Deck B | Deck C |
|---|---|:---:|:---:|:---:|
| 1 | Cost monitoring | ⭐ | | |
| 2 | Performance optimisation | ⭐ | | |
| 3 | SW/HW double-check | | ⭐ | ✓ |
| 4 | Error recovery (rollback) | | ⭐ | ✓ |
| 5 | Pre-training (Burn-in) | | | ⭐ |
| 6 | Crypto signing | ⭐ | ✓ | ✓ |
| 7 | Health check (Patrol) | ⭐ | | ✓ |
| 8 | Agent identity | | ✓ | ⭐ |
| 9 | Compliance automation | ⭐ | | ⭐ |

⭐ = primary message · ✓ = supporting

---

## 영업 도구

각 deck 부록에 포함됨:
- **1-Page Executive Summary** (한 페이지 요약)
- **경쟁사 대응표** (one-liner 답변)
- **체크리스트** (영업 미팅 점검 항목)
- **Demo script** (Deck B), **Compliance 매핑 워크시트** (Deck C)

---

## 다음 step

1. **3 deck 모두 internal review** (3 인 dry-run 후 피드백)
2. **첫 customer 미팅** 진행 (deck 1 개 선택)
3. **결과 기반 deck 갱신** (실제 받은 질문으로 Q&A 강화)

---

## Patent / 자료

- US Provisional `ATV_v7_10` (40 claims)
- `docs/PATENT_SUPPLEMENT_v3.md` (Claims 41-57)
- `docs/WHITEPAPER_PERFORMANCE_KR.md` (한글 기술 백서)
- `docs/MANUAL_v2.2.md` (한글 사용자 매뉴얼, 1070줄)
- `WHITEPAPER.md` (English 백서)
