# AegisData Design Partner Program — Coding AI

> **30-day pilot. No fee. We pick 3 design partners.**

---

## Who we're looking for

**Companies that use Coding AI tools heavily** —
Claude Code, Cursor, Devin (Cognition AI), Replit Agent, GitHub
Copilot Workspace, Sourcegraph Cody, Codeium / Windsurf,
Continue.dev — and run them in one of these ways:

* 사내 50+ 개발자가 매일 사용
* 자율 (autonomous) coding agent 가 production 코드 수정
* 여러 agent 가 협력하는 multi-agent dev 시스템
* AI 도구가 prod DB / API 자격증명에 접근 가능

---

## What you get (30 days, $0)

1. **AegisData sidecar** — 모든 coding agent tool call 검증 + audit chain +
   rollback strategies. ATMU 2-Phase Commit, Ed25519 + Merkle audit, AES-GCM
   encrypted journal, AuditPatrol, Compliance evidence (SOC 2 / EU AI Act /
   HIPAA / ISO 42001).
2. **1-day onboarding workshop** — 현장 또는 원격
3. **Weekly review** — 4 회, 각 30분
4. **Closing report** — 정량 KPI + 다음 step 권고

---

## What we ask in return

1. **Incident 데이터** — anonymized, 5 건 이상
2. **Public case study** — 회사 동의 시 (anonymous OK)
3. **Reference customer** — 다른 prospect 영업 시

NDA + DPA 별첨. ATV body (prompt 내용) 는 귀사 측 저장, Aegis 가 보지 못함.

---

## Self-assessment — 5 questions

다음 5 개 중 **3 개 이상** 해당하시면 fit 입니다:

* ☐ 지난 6 개월 내 \"agent 가 잘못된 코드 / 명령을 production 에 실행\" 한
  incident 가 있었다
* ☐ Engineering 팀이 사내 AI 도구 비용을 정확히 모른다 (per-team /
  per-engineer / per-repo 분리 안 됨)
* ☐ \"누가 어떤 agent 를 어디 deploy 했는지\" 일관되게 추적 못한다
* ☐ Compliance / 보안 review 가 AI agent 채택의 bottleneck 이다
* ☐ Multi-agent system 에서 capability 권한 escalation 이 우려된다

---

## What you can expect from the first meeting

**30 분.** Discovery 미팅 — 우리가 fit 인지 양사 결정.

1. (5분) Coding AI 도구 사용 현황 — 어떤 도구, 어떤 규모, autonomous 사용 여부
2. (15분) Pain discovery — 5 가지 영역 중 가장 painful 한 것 1 개
3. (5분) Aegis 가 그 1 개 영역에서 무엇을 하는지 live demo
4. (5분) Pilot 진행 조건 합의 또는 fit 아님 결정

**Pilot 합의서는 다음 미팅에서 (1주 내).**

---

## Aegis 의 차별화 (다른 보안 도구 대비)

| | Lakera | Helicone | Datadog LLM | Cisco RIA | **Aegis** |
|---|---|---|---|---|---|
| Prompt injection 검출 | ⭐ | | | ⭐ | partial |
| Cost logging | | ⭐ | ⭐ | | partial |
| **Cost attestation 별도 키** | | | | | ⭐ Claim 34 |
| **ATMU 2PC + rollback** | | | | | ⭐ Claim 2/15 |
| **Identity escalation 차단** | | | | | ⭐ Claim 56 |
| **HW/SW double-check** | | | | | ⭐ Claim 26/27 |
| **Background patrol** | | | | | ⭐ Claim 54 |
| **Compliance 자동 매핑** | | | partial | partial | ⭐ Claim 57 |

→ Aegis 와 위 도구들은 **complementary** — 같이 deploy.

---

## Numbers (현재 stack)

* **57 patent claims** (US Provisional ATV_v7_10 + 17 supplements)
* **1177 unit tests** PASS, mypy 125 source files clean
* 10+ public release tags (v2.0.0 ~ v4.4.0)
* 4 compliance frameworks 자동 매핑 (29/31 controls)
* 8 HW source aggregator (PMU/EDAC/IOMMU/NIC/NVML/BMC/TEE/FPGA)
* p99 latency: 10ms sidecar overhead

---

## Get in touch

* **Email**: [TODO: 영업 lead 이메일]
* **LinkedIn**: [TODO: 본인 프로필 링크]
* **GitHub**: <https://github.com/happyikas/Aegis-ATV>

**응답 시간**: 24 hours.
**첫 미팅**: 30 분, 1 주 내 가능.

---

## FAQ

**Q. 우리는 이미 Lakera 쓰고 있어요.**
A. Lakera 는 prompt injection 전문. 우리는 *agent action* layer (ATMU 2PC,
audit, rollback). 다른 layer 입니다 — 같이 deploy 가능.

**Q. Source code 가 open 인가요?**
A. 현재 GitHub private. Pilot 기간엔 source access + on-prem deploy 가능.
OSS 전환은 v5.x 검토 중.

**Q. Pilot 종료 후 무엇이 남나요?**
A. (a) 30 일 audit chain — 귀사 자산. 우리 시스템 없어도 검증 가능.
(b) Compliance evidence packet — 다음 SOC 2 audit 에 사용.
(c) Cost monitoring report — 절감 영역 식별.

**Q. 한국 회사 / 글로벌 회사 모두 가능한가요?**
A. 네. 한국 법인 + 한국어 자료 1070 줄 manual 보유. 글로벌 customer
시 미국 법인 설립 가능.

**Q. 결제 통화 / 가격은?**
A. Pilot 무료. Paid 전환 시 \$30K (50 dev) ~ \$200K (1000+ dev) annual.
Pilot 첫 customer 는 50 % discount.

---

*Last updated: 2026-04-29*
