# Design Partner Playbook — Coding AI Vertical

**버전:** 2026-04-29
**대상 vertical:** Claude Code / Codex / Cursor / Devin / Replit Agent /
Cody 등 **Coding AI 도구를 사용하거나 만드는 회사**
**목표:** 6 개월 내 첫 유료 design partner 1 건 close
**독자:** AegisData 공동 창업자 / 영업 lead / advisor

---

## 0. 한 줄 — 왜 Coding AI vertical 인가

> Coding AI 의 buyer 는 **본인이 매일 그 도구를 사용**한다. agent 가 prod
> 코드 망가뜨린 사고를 *이미* 경험했거나 *경험할 두려움이 큰* 상태.
> Pain 이 즉각적이고 visible — \"agent rm -rf my prod DB\" 시나리오를
> 5 초 안에 이해. 다른 vertical 보다 영업 cycle 이 **짧고**
> Engineering 예산에서 결정되어 Compliance bottleneck 적음.

---

## 1. Vertical 정의 — 누가 우리 customer 인가

### 1.1 두 segment

```
Segment A — AI Coding Tool 회사 (B2B vendor)
─────────────────────────────────────────────
  Cursor / Cognition AI(Devin) / Replit / Sourcegraph(Cody) / 
  Codeium-Windsurf / Continue.dev / Augment / Anthropic(Claude Code)
  
  → Aegis 를 \"enterprise compliance layer\" 로 OEM bundling
  → Pilot: vendor 의 enterprise customer 1 곳에 함께 deploy
  → Deal size: revenue share or per-seat license
  
Segment B — Coding AI heavy user 기업 (enterprise customer)
─────────────────────────────────────────────────────────
  Stripe / Vercel / Notion / Linear / Atlassian / Databricks /
  네이버 / 카카오 / 토스 / 우아한형제들 / 쿠팡 / 라인 /
  현대차 그룹 IT / 삼성SDS / LG CNS
  
  → Aegis 를 자체 internal coding agent 운영의 trust layer 로
  → Pilot: 한 팀의 agent fleet 에 sidecar deploy
  → Deal size: annual enterprise license
```

→ **Segment B 우선 권장**. 의사결정 단순 (단일 회사 buyer), pilot 빠름,
revenue share 협상 불필요.

### 1.2 Buyer persona — Segment B

| Persona | 역할 | Aegis 가치 인지 속도 |
|---|---|---|
| **VP Engineering / CTO** | 도구 도입 결정 | ★★★★★ — 본인이 사용 |
| **Platform Engineering Lead** | 사이드카 운영 책임 | ★★★★★ — \"deploy 해도 부담 없는가?\" |
| **CISO / Security Engineering** | 보안 review 통과 | ★★★★ — 빠른 인지, ATMU + audit chain |
| **Engineering Manager** | 팀의 agent 운영 | ★★★★ — \"내 팀의 agent 가 뭘 했나?\" |
| **CFO / FinOps** | AI 비용 관리 | ★★★ — 두 번째 미팅에 등장 |
| **Compliance Officer** | SOC 2 / EU AI Act | ★★ — 6 개월 사이클 |

**첫 미팅 타겟:** **Platform Engineering Lead** 또는 **VP Eng**.
짧은 미팅, 빠른 결정.

---

## 2. 7 개 영업 artifact

### 2.1 Artifact #1 — Design Partner Program 공식 페이지

**용도:** GitHub repo 의 public 랜딩, 외부 공유 가능

```markdown
# AegisData Design Partner Program — Coding AI

## 누구를 찾고 있나

Coding AI 도구 (Claude Code, Cursor, Devin, Replit Agent, GitHub
Copilot Workspace, Cody 등) 를 다음 중 하나의 방식으로 운영하는 회사:

* 사내 50+ 개발자가 매일 사용
* 자율 (autonomous) coding agent 가 production 코드 수정
* 여러 agent 가 협력하는 multi-agent dev 시스템
* AI 도구가 prod DB / API 자격증명에 접근 가능

## 우리는 무엇을 제공하나 (30일, 무료)

1. **Aegis sidecar** — 모든 agent tool call 검증 + audit + rollback
2. **온보딩 워크숍** — 1일, 우리 엔지니어가 현장 또는 원격
3. **주간 검토** — 4 주간, 발견된 incident + 개선 제안
4. **종결 보고서** — 정량 KPI + 다음 단계 권고

## 우리가 받는 것

1. **Incident 데이터** — 5건 이상 (anonymized)
2. **Public case study** — 회사 동의 시
3. **Reference customer** — 다른 partner 영업 시

## 적합 여부 — 자가 진단 5 문항

1. ☐ 지난 6 개월 내 \"agent 가 잘못된 코드 commit\" 한 incident 가 있다
2. ☐ Engineering 팀이 AI 도구 비용을 정확히 모른다
3. ☐ \"누가 어떤 agent 를 어디 deploy 했는지\" 추적 못한다
4. ☐ Compliance / 보안 review 가 agent 채택의 bottleneck 이다
5. ☐ Multi-agent system 의 capability 권한 escalation 이 우려된다

3개 이상 해당 시 fit. 연락: 

## 대상이 아닌 경우

* Coding AI 미사용 회사
* 5명 이하 startup (당장은 fit 적음)
* 정부 / 공공 / 군수 (별도 sovereign program)

## 응답 시간

- 첫 24 시간 내 답신
- 첫 미팅 30 분, 1 주 내 가능
```

→ `docs/DESIGN_PARTNER_PROGRAM.md` 로 별도 publish (아래 §11).

### 2.2 Artifact #2 — Outreach 템플릿 3 종

#### 2.2.1 Cold outreach (LinkedIn DM, 80 자 이내)

```
[이름]님 안녕하세요. AegisData 의 [본인 이름] 입니다.

귀사가 Cursor + Devin 을 사내에서 적극 사용 중이신 것으로 알고
있는데, agent 의 production 변경에 대한 audit / rollback 인프라가
어떻게 운영되는지 궁금합니다.

저희는 Anthropic Claude Code 의 ATV-2080 기반 사이드카 (40-claim
US 임시특허) 를 만들고 있고, 30 일 무료 design partner 로 1건의 
Fortune 500 을 찾고 있습니다. 5 분 정도 미팅 가능하실까요?

GitHub: github.com/happyikas/Aegis-ATV
```

**핵심:**
- 50–80 자, 첫 5 줄에 결론
- 회사명 specific 하게 인용 (Cursor + Devin 등)
- \"Fortune 500 찾고 있다\" — exclusivity feel
- 5 분 — 거절 비용 낮게

#### 2.2.2 Warm intro 받은 후 첫 이메일

```
Subject: [Mutual contact] 소개 — Aegis sidecar 30일 design partner

[이름] 님,

[Mutual contact] 께서 귀사의 platform team 이 사내 coding agent
운영의 audit / rollback 인프라를 고민 중이라고 알려주셨습니다.

저희 AegisData 는 Anthropic Claude Code + Cursor + Codex 의 도구
호출을 통과시키는 사이드카로, 다음을 자동 보장합니다:

1. ATMU 2-Phase Commit — agent 의 destructive action 사전 차단 + rollback
2. Identity + delegation chain — manager agent → worker agent 권한 escalation 차단
3. Cost attestation — 회계 감사인이 prompt 안 보고 비용만 검증
4. SOC 2 / EU AI Act evidence 자동 생성

지난 4 주 사이 출시된 v4.0 ~ v4.4 가 이 vertical 의 4 개 핵심 pain
을 모두 cover 합니다.

30 분 미팅 가능하시면 다음 주 (5/5–5/9) 화/수/목 어느날이든 가능
합니다. 30 일 free pilot, NDA + DPA template 모두 ready.

— [본인 이름]
GitHub: happyikas/Aegis-ATV
링크드인: ...
```

#### 2.2.3 Conference / 발표 후 follow-up

```
Subject: [컨퍼런스명] 발표에서 인사 — Aegis sidecar 후속

[이름] 님,

오늘 [발표 제목] 발표 중 \"agent 가 prod 에 commit 한 사고\" 언급하신
부분 정말 공감했습니다. 사실 저희가 만들고 있는 게 정확히 그 시나리오를 위한 것이라 짧게 follow-up 보냅니다.

GitHub repo: github.com/happyikas/Aegis-ATV
30 일 design partner program: docs/DESIGN_PARTNER_PROGRAM.md

다음 주 30 분 데모 미팅 가능하실지요? 팀 분 1–2 분 모시고
Aegis 가 실제로 어떻게 incident 를 잡는지 live 시연합니다.

— [본인 이름]
```

### 2.3 Artifact #3 — Pilot 합의서 template

**구조:** 1 페이지 LOI + 별첨 NDA + 별첨 DPA

```markdown
# Aegis Design Partner Pilot — Letter of Intent

**Effective:** [날짜]
**Pilot 기간:** 30 일 (mutually extensible to 60 일)
**비용:** $0 (Aegis 무상 제공)

## 1. Scope

AegisData 는 [Customer] 의 [팀 / cluster / project] 에서 동작하는
[도구 — Claude Code / Cursor / 등] 의 도구 호출에 대한 사이드카
deploy 를 30 일간 무상 제공한다.

## 2. Aegis 의 의무

- 1일 온보딩 워크숍 (현장 또는 원격)
- 주간 검토 미팅 ×4 (각 30분)
- Incident 발생 시 24h 내 응답
- 30 일 종결 시 KPI 보고서 + 권고안 제출

## 3. Customer 의 의무

- 사이드카 deploy 환경 (staging or prod) 제공
- 주간 미팅 1 명 이상 참석
- Incident 발생 시 anonymized 데이터 공유 동의
- 종결 시 case study 공개 여부 검토 (거절 가능)

## 4. 데이터 처리

- 별첨 DPA (Data Processing Agreement) 적용
- ATV body (prompt 내용) 는 **Customer 측에 저장**, Aegis 가 보지 못함
- Cost ledger / patrol report / incident 통계는 Aegis 와 공유 (anonymized)

## 5. 비밀유지

- 별첨 NDA 적용
- Pilot 종료 후 12 개월간 양사 보유 정보 공개 금지
- 단, Aegis 는 \"이 회사가 design partner 였음\" 을 명시할 권리
  (회사명 공개는 별도 동의)

## 6. 종결 후 옵션

(a) 유료 전환: annual enterprise license
(b) 추가 pilot 연장 30 일
(c) 종료 + case study 공개 (회사 익명 가능)

## 7. 분쟁

- 한국 법
- 서울중앙지방법원 전속관할

## 8. 서명

Aegis: ___________ Customer: ___________
[이름]              [이름]
[직위]              [직위]
[날짜]              [날짜]
```

→ 별첨 NDA 와 DPA 는 한국 / 글로벌 표준 template 사용 (KISA NDA 표준 +
GDPR Art. 28 DPA 모델 조항).

### 2.4 Artifact #4 — Target customer list

#### 2.4.1 한국 (segment B 우선)

| 회사 | 이유 | 예상 buyer |
|---|---|---|
| **네이버** | HyperCLOVA 자체 + Cursor / Copilot 사내 채택 | 클로바 platform lead, Naver Cloud CISO |
| **카카오** | KakaoBrain Karlo + 사내 GitHub Copilot | 개발 platform lead |
| **토스 / 비바리퍼블리카** | 자체 AI 팀 + 금융 compliance 압박 | 토스증권 CTO, 토스플레이스 platform |
| **우아한형제들 (배민)** | 사내 dev 도구 적극 채택 + 연 영업이익 압박 | woowahan-tech 개발 lead |
| **쿠팡** | Cursor / Devin 사내 deploy + 미국 SOX | platform engineering 임원 |
| **라인** | 일본 LY corp + 글로벌 fintech | Line Plus tech lead |
| **삼성 SDS** | 사내 AI 도구 + 외부 SI 둘 다 | AI/Cloud 사업부 임원 |
| **LG CNS** | LG Group 의 AI 운영 책임 | DX 본부 임원 |
| **현대자동차 그룹 IT** | 자율주행 + 내부 dev 양면 | Hyundai AutoEver, 현대 NGV |
| **당근마켓** | 사내 적극 GitHub Copilot + 빠른 dev cycle | engineering lead |

#### 2.4.2 글로벌 (segment B)

| 회사 | 이유 | 진입 경로 |
|---|---|---|
| **Stripe** | Cursor 적극 + SOX-grade audit 압박 | engineering blog 저자, conf 발표자 |
| **Vercel** | Anthropic Claude Code partnership | DevRel 통한 warm intro |
| **Linear** | small team, big AI agent usage | founder 직접 |
| **Notion** | 사내 AI 적극 + cost monitoring 부재 | platform team |
| **Atlassian** | Bitbucket + Jira AI + enterprise compliance | EU AI Act readiness 영업 |
| **GitLab** | open core + AI features | DevSecOps 팀 |
| **Databricks** | 자체 LLM Mosaic + 고객사 위해 audit 필요 | enterprise platform |
| **Snowflake** | AI Cortex + customer audit 압박 | trust & compliance team |
| **Figma** | 사내 AI 적극 + new AI features 출시 중 | platform engineering |
| **Anthropic 자체** | Claude Code 의 enterprise compliance layer | Anthropic Enterprise 팀 |

#### 2.4.3 Coding AI tool vendor (segment A — 후순위)

| Vendor | 협업 형태 |
|---|---|
| **Cursor (Anysphere)** | enterprise 고객용 trust layer OEM |
| **Cognition AI (Devin)** | autonomous agent 의 audit 의무 충족 |
| **Replit (Replit Agent)** | enterprise tier 추가 sell |
| **Sourcegraph (Cody)** | 이미 enterprise focus, 좋은 fit |
| **Continue.dev** | open source, partnership easier |

→ **첫 8 주는 segment B 의 한국 4 사 + 글로벌 4 사 = 8 곳 우선**.

### 2.5 Artifact #5 — Discovery question 가이드 (첫 30분 미팅용)

```
== 도입부 (5분) ==

1. \"귀사가 사내에 어떤 coding AI 도구를 deploy 하셨나요?\"
   - Claude Code / Cursor / Codex / Devin / Cody / Continue / Copilot
   - 사용 인원 규모 (5/50/500)
   - autonomous mode 사용 여부 (사람 in-the-loop vs 완전 자동)

2. \"평균 도구당 일일 호출 양 (대략) 은?\"
   - <100 / 100-1000 / 1000+ / 만+
   → cost monitoring 가치 척도

== Pain discovery (15분) ==

3. \"agent 가 잘못된 코드 / 명령을 실행한 incident 있었나요?\"
   - 빈도 (월 1회 / 분기 1회 / 연 1회)
   - 영향 (rollback 가능했나? 어떻게?)
   - 누가 발견했나 (자동 / 수동 / customer)
   → ATMU 2PC 가치 척도

4. \"agent 가 다른 agent 를 호출하는 multi-agent 시스템 운영하시나요?\"
   - Yes → 권한 / 격리 어떻게?
   - No → 가까운 미래 예정인가?
   → DelegationChain (Claim 56) 가치

5. \"AI 도구의 비용 control 은 어떻게 하시나요?\"
   - 회사 전체 monthly bill 만 보는가?
   - Per-team / per-repo / per-engineer 분리되는가?
   - Surprise bill 경험?
   → Cost attestation 가치

6. \"compliance team 이 AI 도구 채택의 bottleneck 인 적 있나요?\"
   - SOC 2 / EU AI Act / HIPAA / 내부 보안 review
   - 어떤 evidence 를 요구받았나?
   → Compliance evidence (Claim 57) 가치

7. \"agent 가 prod DB / API 자격증명에 접근하나요?\"
   - secret rotation 어떻게?
   - audit trail 보유?
   → identity + step308 가치

== Solution mapping (5분) ==

8. \"위 5 개 영역 중 어느 것이 가장 painful 한가요?\"
   - Top 1 만 선택 받음
   - 그것에 맞는 deck (A/B/C) 또는 demo 만 보여줌

9. \"Aegis 사이드카 deploy 30일 free pilot — 시작 가능한 환경이 있나요?\"
   - staging cluster / 한 팀 / 한 service
   - 어떤 도구 (Claude Code / Cursor / etc)
   - decision maker 추가 미팅 필요?

== 마무리 (5분) ==

10. \"다음 step\" 합의:
    - Yes → pilot 합의서 보내드림 (1주 내)
    - Maybe → 더 큰 미팅 (engineering team 5분)
    - No → 거절 이유 기록 (다른 partner 영업 시 학습)
```

### 2.6 Artifact #6 — Pilot 성공 KPI 프레임워크

```
== 30일 Pilot KPI ==

★ Hard KPIs (정량) ★
1. agent 도구 호출 수: ____ / 일 (Aegis 통과한 호출)
2. BLOCK 비율: ____% (Aegis 가 차단한 호출)
3. REQUIRE_APPROVAL 비율: ____%  
4. 발견된 incident 수: ____ 건
5. Rollback 수행: ____ 건 (성공 / 실패)
6. Cost attestation 정확도: ____ % (vendor bill 대비 예측 정확도)
7. Audit chain 무결성 (patrol findings): ____ 건 critical / warning
8. 평균 latency 추가: ____ ms (목표 <10ms)

★ Soft KPIs (정성) ★
9. Engineering team 만족도 (1-5): ____
10. Compliance team 만족도 (1-5): ____
11. \"deploy 가능한 production 수준\" 평가 (1-5): ____
12. 추가 feature 요청 (top 3)

== 60일 / 90일 마일스톤 ==

[60일] If pilot 연장
- KPI 재측정
- Custom feature 1-2 개 협의
- Pricing 미팅 진행

[90일] 결정 시점
- 유료 전환: annual license 협상
- 종료: case study 공개 동의 받기
- 연장: 추가 pilot 90일 (rare case)

== 종결 보고서 구조 ==

1. Executive summary (1 page)
2. KPI dashboard (12개 metric)
3. 발견된 incident 5건 deep-dive
4. 개선 제안 top 5
5. ROI 정량 추정 (annual)
6. 다음 step 옵션 3 가지
```

### 2.7 Artifact #7 — Case study template (종결 후 공개)

```markdown
# Case Study: [Customer 이름 또는 \"Fortune 500 SaaS\"]

## 회사 소개
[2-3 문장. 산업 / 규모 / coding AI 사용 패턴]

## 도전 과제
[Pain point — 우리 #1-7 가치 중 어떤 것이 그들의 가장 큰 문제였나]

## Aegis Pilot — 30일

### 배포 환경
- 사이드카 deploy: [staging / prod / specific cluster]
- 검증한 coding AI 도구: [Cursor / Devin / Claude Code]
- 활성화한 Aegis 컴포넌트: [step308 identity / ATMU / patrol / compliance]

### Setup time
- Day 0: 워크숍 1일
- Day 1-3: integration
- Day 4: production traffic 통과 시작
- Day 30: 종결

## 결과 — 정량

### 보안 / 안전
- 차단된 destructive action: ___ 건 (avoided $___ damage)
- Rollback 실행: ___ 건
- Identity escalation 차단: ___ 건

### 비용
- Pre-Aegis monthly AI bill: $___
- Post-Aegis (visibility 후 최적화): $___
- 절감률: ___%

### Compliance
- SOC 2 evidence 자동 생성: ___시간 → 1시간 미만
- 분기별 audit prep 시간: ___% 단축

## 결과 — 정성

\"[Customer 측 quote 1]\"
— [Customer Engineering VP]

\"[Customer 측 quote 2]\"
— [Customer CISO]

## 다음 step

[Customer] 는 30일 pilot 후 [paid tier / extended pilot / production
rollout] 으로 진행. [추가 detail].

## Aegis 가 무엇을 배웠나

[Customer 의 feedback 으로 우리가 추가한 feature / 개선한 영역]

---

*Published [날짜] with permission of [Customer]. All financial
figures normalized.*
```

---

## 3. 영업 motion 의 *현실적인* conversion 비율

```
                                   비율 가정       30일 후      
   ┌───────────────────────┐                                
   │  Outreach 보낸 수     │  100   ─────►  100             
   └───────────┬───────────┘                                
               │ 응답 ~10–20 % (warm intro 시 30–50 %)        
               ▼                                              
   ┌───────────────────────┐                                
   │  답신 받은 수         │  15                             
   └───────────┬───────────┘                                
               │ ~50 % 가 30 분 미팅 동의                     
               ▼                                              
   ┌───────────────────────┐                                
   │  Discovery 미팅 한 수 │   8                             
   └───────────┬───────────┘                                
               │ ~25 % 가 strong fit                          
               ▼                                              
   ┌───────────────────────┐                                
   │  Pilot 합의서 보낸 수 │   2                             
   └───────────┬───────────┘                                
               │ ~50 % 가 sign                                
               ▼                                              
   ┌───────────────────────┐                                
   │  Pilot 시작 수        │   1                             
   └───────────────────────┘                                
   
   → 100 outreach → 1 pilot. 이게 baseline.
   
   warm intro / conf-driven 은 5–10× 효율적:
   → 20 warm intro → 1 pilot
```

**개선 levers (높은 ROI 순):**
1. **Conference 발표** — 1번 발표 = 100 cold outreach 효과
2. **Mutual contact warm intro** — 응답률 10× 상승
3. **Public live demo** — \"이거 우리한테 됩니다\" 즉각 가시성
4. **Engineering blog 게시** — SEO + 신뢰도

---

## 4. 첫 6 주 실행 plan

### Week 1 — Pre-launch (이번 주)
- [ ] Playbook 검토 + 수정 (이 문서)
- [ ] Outreach 템플릿 3 종에 본인 이름 / 회사 fact 채움
- [ ] DESIGN_PARTNER_PROGRAM.md 별도 페이지로 publish
- [ ] LinkedIn 프로필 업데이트 (\"Founder, AegisData. 30 day design partner program for Coding AI\")
- [ ] 첫 8 후보 회사 / 사람 이름 식별 (위 §2.4 에서 4 한국 + 4 글로벌)

### Week 2 — First wave outreach
- [ ] Mutual contact 매핑 (각 후보 회사에서 누구 아는가? 0 명이면 cold)
- [ ] Warm intro 8 건 요청 (상호 contact 에게 부탁)
- [ ] Cold DM 16 건 발송 (각 회사 platform lead 2 명씩)
- [ ] 응답 tracking 시작 (간단 Notion / 스프레드시트)

### Week 3 — 첫 미팅 wave
- [ ] 응답 받은 5–8 후보와 30분 미팅 schedule
- [ ] Discovery question 가이드 (§2.5) 사용
- [ ] 미팅 후 24h 내 follow-up 이메일 (notes + Pilot 합의서 첨부)

### Week 4 — Conference 발표 신청
- [ ] [Conference 명] CFP 제출 (e.g., 2026 Q3 PyCon Korea, KISA AI 컨퍼런스, NeurIPS workshop)
- [ ] 발표 주제: \"Cursor / Devin 의 production audit 인프라 — open source AegisData 의 6 개월 학습\"

### Week 5–6 — Pilot 합의 wave
- [ ] 2 후보로 좁혀지면 pilot 합의서 보냄
- [ ] NDA + DPA 검토 (양사 법무)
- [ ] 첫 sign → onboarding workshop 일정

---

## 5. 영업 미팅 자주 받을 질문 + 답변

> **Q: 우리 이미 Lakera / Cisco RIA / Datadog 쓰고 있는데?**
> A: Lakera 는 prompt injection 검출 (그게 잘함). 우리는 *agent action* 의
> ATMU 2PC + audit + cost attestation. **다른 layer 임 — 같이 deploy.**

> **Q: 무료 pilot 인데 우리 데이터 보호는?**
> A: ATV body (prompt 내용) 는 **귀사 측에 저장**, 우리가 보지 못함.
> 별도 cost-attestation 키 (Claim 34) 로 비용만 검증 가능. NDA + DPA 별첨.

> **Q: latency 추가는?**
> A: p99 ~10 ms (sidecar). 도구 자체 latency 가 50–500 ms 이라 <2 % 오버헤드.

> **Q: pilot 종료 후 무엇이 남나?**
> A: (a) 30일 동안 모인 audit chain — 귀사 자산. 우리 시스템 떠나도 검증 가능.
> (b) compliance evidence packets — 다음 SOC 2 audit 에 사용 가능.
> (c) cost monitoring report — 절감 영역 식별.

> **Q: code 가 open source 인가?**
> A: GitHub repo 는 private (special access 가능). 향후 OSS 전환 검토 중.
> Pilot 기간엔 source access + on-prem deploy 가능.

> **Q: Anthropic / OpenAI 가 똑같은 거 만들면?**
> A: model provider 가 본인 모델의 audit 만든다 = conflict of interest.
> 독립 third-party sidecar 가 표준 (FedRAMP / SOC 2 도 그렇게 요구).

> **Q: 한국 법인 vs 미국 법인?**
> A: 현재 한국 법인. 글로벌 customer 시 미국 법인 설립 가능.

> **Q: 실 silicon (TDX/SEV-SNP) 환경에서 검증됐나?**
> A: 정직하게: v4.4 코드는 ready, **production deployment 1건 = 0 건**.
> 귀사가 그 첫 번째 가 되면 case study 의 unique selling point 입니다.

---

## 6. 영업 데이터 tracking template

```
| 회사 | Contact | 단계 | 마지막 액션 | 다음 액션 | 날짜 |
|---|---|---|---|---|---|
| 네이버 | OOO 이사 | outreach 완료 | warm intro 요청 | 응답 대기 | 4/29 |
| 토스 | XXX 팀장 | discovery | 30분 미팅 완료 | pilot 합의서 보냄 | 5/3 |
| Stripe | YYY VP | response | 첫 답신 | 미팅 schedule | 5/5 |
```

→ 간단 Notion / Airtable / Google Sheet. 영업 lead 는 매일 5 분만 업데이트.

---

## 7. 정직한 risk 와 mitigation

### Risk 1: 0 건 close
- **확률**: 30 % (cold start 의 일반 비율)
- **Mitigation**:
  - Conference 발표 1 건 (warm-intro pipeline 생성)
  - Open source 일부 release (community-driven inbound)
  - 첫 80 outreach 후 conversion 0 면 메시지 재검토

### Risk 2: pilot 시작했는데 고객사 이슈로 종료
- **확률**: 50 % (pilot → paid 전환은 절반 이하)
- **Mitigation**:
  - KPI 명확히 30일 전 합의
  - 주간 sync 게으르지 않게
  - case study 라도 받아냄 (anonymized OK)

### Risk 3: 경쟁사가 더 빨리 close
- **확률**: 40 % (Lakera / Robust Intelligence 자금 우위)
- **Mitigation**:
  - 차별화 메시지 (ATMU 2PC + cost attestation = 그들이 못함)
  - 한국 시장 우선 (그들이 약함)

### Risk 4: paid 전환 협상 실패
- **확률**: 30 %
- **Mitigation**:
  - Pricing 미리 결정 ($30K–$200K annual range)
  - Discount 협상 권한 분명히

---

## 8. \"무엇을 안 함\" — 영업 안티 패턴

❌ **이번 단계에 하지 말 것:**

1. **여러 vertical 동시 영업** — Coding AI 만. compliance / 의료 / 금융 다음.
2. **메시지 9 가치 모두 강조** — Top 1 만. 미팅마다 다를 수 있음.
3. **OEM partnership 우선 영업** (segment A) — segment B reference 먼저 확보 후.
4. **수익 협상 빠르게** — 첫 close 까지 무료 pilot. paid 는 나중.
5. **글로벌 우선** — 한국 1 건 reference 가 글로벌 영업의 무기.
6. **전체 deck 발송** — 30 분 미팅에 80 슬라이드 보내지 말 것.

---

## 9. Open questions (사용자 결정 필요)

1. **첫 conference 어떤 걸 노리시나?**
   - 한국: PyCon Korea, KISA AI 컨퍼런스, 모두의연구소
   - 글로벌: NeurIPS Workshop, USENIX Security, Apple WWDC
   
2. **Pricing 첫 제시 가격은?**
   - 추천: $30K (Tier 1, 50 dev) / $80K (Tier 2, 250 dev) / $200K (Tier 3, 1000+)
   - Pilot 종료 후 첫 paid 는 50 % 할인 — reference 가치 보상

3. **법무 paperwork 누가?**
   - 한국: 본인 + 외부 법무 (서울중앙지방법원 관할 default)
   - 글로벌: 미국 법인 설립 시점 결정

4. **무료 pilot 의 Aegis 인력 cost 한계는?**
   - 추천: 첫 3 pilot 까지만 무료 + 본인 ~50 % 시간 투입
   - 4번째 부터 paid (단가 무관 — 유료 commitment 가 핵심)

---

## 10. 다음 한 step

**이번 주 끝까지:**
- [ ] 8 후보 회사 / 사람 이름 LinkedIn 에서 식별 (위 §2.4)
- [ ] Mutual contact 0 명 vs 1 명 이상 표시
- [ ] 1 명 이상 contact 있는 4 회사부터 outreach 시작
- [ ] DESIGN_PARTNER_PROGRAM.md publish (GitHub repo README 에 link)

**그 후 매주:**
- [ ] 매주 월요일: 응답 tracking 검토 → 다음 액션 결정
- [ ] 매주 금요일: Aegis 측 weekly retro (영업 + 코드)

---

## 11. 부록 — DESIGN_PARTNER_PROGRAM.md (publish-ready)

별도 파일로 저장 → GitHub repo README 의 \"Get involved\" 섹션에 link.

```markdown
# AegisData Design Partner Program — Coding AI

We're looking for **Fortune 500 companies that use coding AI tools
heavily** (Claude Code, Cursor, Devin, Replit Agent, GitHub Copilot
Workspace) to be our first design partners.

## Who fits

Coding AI 도구를 다음 중 하나의 방식으로 운영하는 회사:

* 사내 50+ 개발자가 매일 사용
* 자율 (autonomous) coding agent 가 production 코드 수정
* 여러 agent 가 협력하는 multi-agent dev 시스템
* AI 도구가 prod DB / API 자격증명에 접근

## What we provide (30 days, free)

1. AegisData sidecar — 모든 agent tool call 검증 + audit + rollback
2. 1-day onboarding workshop (현장 또는 원격)
3. 4 weekly review meetings (각 30분)
4. 종결 보고서 + 다음 step 권고

## What we ask

1. Incident 데이터 (anonymized) 5 건 이상
2. Public case study (회사 동의 시 — anonymous OK)
3. Reference customer 가능

## Self-assessment — 5 questions

3개 이상 해당하시면 fit 입니다:

1. ☐ 지난 6 개월 내 \"agent 가 잘못된 코드 commit\" 한 사건이 있다
2. ☐ Engineering 팀이 AI 도구 비용을 정확히 모른다
3. ☐ \"누가 어떤 agent 를 어디 deploy 했는지\" 추적 못한다
4. ☐ Compliance / 보안 review 가 agent 채택의 bottleneck 이다
5. ☐ Multi-agent 의 capability escalation 이 우려된다

## Get in touch

Email: [your email]
LinkedIn: [your linkedin]
GitHub: github.com/happyikas/Aegis-ATV

응답 시간: 24 hours.
첫 미팅: 30 분, 1 주 내 가능.
```

---

**문서 끝.**
다음 step: §10 의 체크리스트 4 개 항목 이번 주 안에 시작.
