# Aegis ATV Architecture — Unit, Storage, Events, Identifiers

**상태**: canonical reference — patent prosecution + due diligence + 외부 영업 모두 이 문서에 합의함.
**최종 갱신**: 2026-05-06
**버전**: v1.0 (PR #100, "ATV unit consolidation")

---

## 0. TL;DR — 한 페이지 요약

| 결정 | 답 |
|------|-----|
| ATV record 의 *생성* 단위 | **per atomic execution step** (≈ per tool invocation) |
| ATV chain 의 *scope* | **(`agent_instance_id`, `session_id`)** 쌍 |
| Tenant / System / Container 는 | ATV 의 *namespace / 식별자 field* — 할당 단위 ❌ |
| ATV 가 *물리적으로 저장*되는 layout | 3-tier — Tenant Vault → AID Partition → Hot Ring Buffer (orthogonal axis) |
| ATV emit event 수 (patent 권장) | **6** — currently 1 emitted, 5 gap (§4) |

> **핵심 분리** — *생성 단위* (agent_instance × session × step) 와 *저장 계층* (3-tier) 은 직교한다. 한 결정이 다른 결정을 대체하지 않는다.

---

## 목차

1. [용어 정의 + 식별자 카탈로그](#1-용어-정의--식별자-카탈로그)
2. [ATV unit — 생성 단위 정의](#2-atv-unit--생성-단위-정의)
3. [ATV chain — 체인 scope](#3-atv-chain--체인-scope)
4. [6 generation events + shipped gap](#4-6-generation-events--shipped-gap)
5. [Storage architecture — 3-tier 직교축](#5-storage-architecture--3-tier-직교축)
6. [5 blind spots + 완화 설계](#6-5-blind-spots--완화-설계)
7. [Solo Free degenerate mode](#7-solo-free-degenerate-mode)
8. [Patent 정렬 — dependent claim draft](#8-patent-정렬--dependent-claim-draft)
9. [Roadmap — gap 별 PR 매핑](#9-roadmap--gap-별-pr-매핑)
10. [부록 — full ATVHeader v2 schema](#10-부록--full-atvheader-v2-schema)

---

## 1. 용어 정의 + 식별자 카탈로그

ATV 시스템에 등장하는 식별자는 **각각 다른 boundary 와 lifecycle 을 가진다.** 정렬을 위한 정의표:

| 식별자 | 무엇을 식별하나 | Lifecycle | Boundary 성격 |
|--------|----------------|-----------|---------------|
| `tenant_id` | 고객사 / 조직 | 계약 기간 (수년) | **privacy + 서명 + 청구** |
| `deployment_id` | 한 deployment (cluster, region) | 배포 cycle | policy + tool registry + model 버전 |
| `runtime_context_id` | 실제 실행 substrate | 컨테이너 lifecycle | **runtime attestation** (TEE quote, container digest) |
| `agent_id` | logical agent role/principal | deployment 기간 | **stable identity** (예: `MedRAG-CKD`) — 라이선스 단위 |
| `agent_instance_id` | stateful agent execution context | 인스턴스 살아있는 동안 (수 분~수 시간) | **state ownership** — context window, working memory |
| `session_id` | 한 task / run | session 종료까지 | **chain anchor** — ATV chain 이 묶이는 키 |
| `step_seq_no` | session 내 step 번호 | within-session monotonic | turn counter |
| `action_txn_id` | tool invocation 의 transaction id | per call | **ATV record 1건의 키** |
| `parent_atv_hash` | 호출 트리에서 부모 ATV | record 간 link | call-tree linkage |

**결정적 통찰**: 위 9개 중 **`agent_instance_id` + `session_id` + `step_seq_no`** 세 개만이 ATV 의 *주체 (subject)* 를 결정한다. 나머지는 *맥락 (context)* 으로 ATV header 에 기록되지만 unit 을 결정하지 않는다.

---

## 2. ATV unit — 생성 단위 정의

> An Agent Telemetry Vector record is generated for **each atomic execution step or proposed tool-invocation transaction** of a particular **agent instance** within a **session**. Tenant, deployment, runtime context, container/TEE identifiers are recorded as **namespace and runtime-attestation fields** within the record, but do not themselves constitute the primary unit of ATV allocation.

### Why per-step, not per-agent

* Agent 1 instance 가 한 session 안에서 여러 *외부 instruction source* 를 ingest 함 (web page, tool output, AGENTS.md, memory entry). 각 step 마다 provenance set + risk posture 가 달라진다.
* Patent Claim 1: "each textual instruction consumed by the agent at each execution step" — provenance 와 cryptographic binding 이 *step 단위*.
* Forensic 단위로도 정확: "어느 agent instance 가 / 어느 session 에서 / 어느 step 에서 / 어떤 instruction source 때문에 / 어떤 tool invocation 을 / 왜 BLOCK 했는가?" 에 직답 가능해야 함.

### Why not per-agent / per-tenant / per-container

* **Per-agent**: 같은 agent 가 step 마다 다른 결정을 하는데 1 record 로 묶이면 forensic granularity 손실.
* **Per-tenant**: 위 5개 질문 중 *어느 agent / session / step* 정보 손실.
* **Per-container**: container 는 *substrate*. 한 container 에 여러 agent instance 가 살 수 있고, 한 agent instance 가 컨테이너 migration 시 chain 끊김 위험.

### Code 정합

| 의견 정의 | 우리 shipped 구현 | 일치 |
|-----------|------------------|------|
| `ATV record = per execution step` | `build_atv()` 가 매 `handle_pretool()` 1회 호출 → 1 PreToolUse = 1 ATV | ✅ |
| `parent_atv_hash` | `prev_hash` (linear chain only) | ⚠️ tree 미지원 (§6 B-2) |
| `step_seq_no` | 외부 (`TemporalContext.history` length) — header 에 없음 | ❌ → 본 PR 에서 추가 |

---

## 3. ATV chain — 체인 scope

ATV chain 은 **`(agent_instance_id, session_id)`** 쌍으로 묶인다. 한 chain 내부에서:

* `prev_hash` / `parent_atv_hash` 가 record 간 cryptographic link 을 형성
* Genesis record 는 session_init event (§4 event 1)
* Terminal record 는 session_end event 또는 process termination

### Cross-chain linkage

여러 chain 이 *interact* 하는 시나리오:

```
Orchestrator chain (instance_id=A1, session_id=S1)
  ├─ step 5: tool call → spawn sub-agent B
  │           ATV[A1, S1, step=5] has action_txn_id = T1, decision=ALLOW
  │           parent_atv_hash points to ATV[A1, S1, step=4]
  │
  └─ Sub-agent chain (instance_id=B1, session_id=S2)
        ├─ step 0: session_init  ← parent_atv_hash points to ATV[A1, S1, step=5] (cross-chain link)
        ├─ step 1: tool call ...
        └─ step N: session_end → emit ATV with closing_to = T1 (parent transaction)
```

→ Chain 내부는 *linear*, chain 간은 *DAG*. 두 가지 link:

1. **Within-chain linear link** — `prev_atv_hash` (record N → record N-1)
2. **Cross-chain delegation link** — `parent_atv_hash` (sub-agent chain root → parent's invocation record)

이 둘이 합쳐 **forest of trees** 구조. Forensic replay 시 어느 incident root 에서 시작하든 *모든 후속 결정* 을 reachable 하게 만듦.

---

## 4. 6 generation events + shipped gap

Patent + RAAM 분석에 의하면, 다음 6 event 마다 ATV record 가 emit 되어야 한다:

| # | Event | Trigger | 현재 emit? | gap |
|---|-------|---------|-----------|-----|
| 1 | **Session initialization** | 새 agent run 시작 — system prompt + AGENTS.md + tool manifest ingest | ❌ | session_init ATV 미구현 |
| 2 | **External instruction ingestion** | web result, tool output, prior-agent output, memory entry → context | ❌ | 외부 instruction 별도 ATV 없음 |
| 3 | **Proposed tool invocation** | Write-Ahead Intent Log 시점, Action Firewall 결정 | ✅ | 현재 유일하게 emit |
| 4 | **Tool result ingestion** | tool 실행 결과가 다시 context 로 들어옴 | ⚠️ | PostToolUse hook 있지만 *간략한 record* (`hook="PostToolUse"`) |
| 5 | **Reviewer attestation** | RAAM 의 reviewer + corroborator agent digest 비교 | ❌ | RAAM 미구현 |
| 6 | **Mutation / re-attestation** | step309 instruction-drift detect, baseline reattest | ⚠️ | drift detect 만, ATV emit ❌ |

→ **5개의 명시적 gap**. 각 gap 은 별도 PR 트랙으로 메워야 함 (§9 roadmap).

### 왜 6 events 가 다 필요한가

| Event | Forensic 의 어떤 질문에 답하나 |
|-------|-------------------------------|
| 1 session_init | "이 agent 는 어떤 system prompt / tools / policies 로 시작했나?" |
| 2 external_ingest | "어떤 외부 컨텐츠가 agent 의 belief 에 영향을 줬나?" — **belief drift attack 추적** |
| 3 tool_invocation | "agent 가 무엇을 시도했나, firewall 이 어떻게 판단했나?" |
| 4 tool_result | "agent 가 받은 응답이 다음 결정을 어떻게 바꿨나?" |
| 5 reviewer_attest | "독립 reviewer 가 합의했나?" — **cross-corroboration** |
| 6 re_attest | "정책이 바뀌었나, 누가 새 baseline 을 서명했나?" |

→ Event 2 (external_ingest) 가 빠지면 **3-week procurement attack** 같은 long-horizon belief drift 사고를 cryptographically reconstructible 하게 추적할 수 없다. PocketOS 9-second incident 같은 것은 event 3 만으로 잡히지만, *재발 방지* 를 위한 root cause 는 event 2 에 있음.

---

## 5. Storage architecture — 3-tier 직교축

§2~4 가 **생성 (when, what, who) 의 정의**라면, 본 §5 는 **저장 (where, how long) 의 정의**. 둘은 직교한다.

### 3-tier storage hierarchy

```
┌────────────────────────────────────────────────────────────────┐
│  Tier 1 — Tenant Vault                                         │
│  • CXL Type-3 namespace (per tenant)                           │
│  • Per-tenant DEK (AES-256-GCM, M15 §13B-1)                    │
│  • Cross-tenant aggregation (PPA v3.2) 의 read boundary         │
│  • Cardinality: 10² (수십~수백 tenant)                          │
│  • Retention: 계약 기간 (수년) — cold archive                   │
└────────────────────────────────────────────────────────────────┘
           │
           ↓
┌────────────────────────────────────────────────────────────────┐
│  Tier 2 — Agent Partition (per logical agent_id)               │
│  • AID-tagged sub-partition (NAND block 또는 SQL partition)    │
│  • Forensic recovery 의 primary unit                            │
│  • 라이선스 / 청구 단위 (per-agent licensing)                    │
│  • Cardinality: 10³ ~ 10⁴ (tenant 당 10~1000 agent)             │
│  • Retention: deployment 기간 — warm storage                    │
└────────────────────────────────────────────────────────────────┘
           │
           ↓
┌────────────────────────────────────────────────────────────────┐
│  Tier 3 — Hot Ring Buffer (per session)                        │
│  • DRAM/SRAM (T3 hardware) 또는 in-process state (T2 software) │
│  • In-flight ATV 의 read-through cache                          │
│  • Power-fail-safe flush → Tier 2                              │
│  • Cardinality: 10⁵ (active session 수, ephemeral)              │
│  • Retention: session 종료까지 (분~시간) — hot storage         │
└────────────────────────────────────────────────────────────────┘
```

### Chain ownership — 단일 진실

> **Tier 2 (Agent Partition) 가 canonical chain owner.**
> Tier 1 은 read-only aggregation view, Tier 3 은 read-through cache.

이렇게 못 박아야 하는 이유:

* Patent 의 cryptographic single-truth chain 가치는 *한 곳에 한 chain* 일 때만 성립.
* PPA v3.2 cross-tenant aggregation 은 Tier 1 read view 로 노출 — Tier 2 chain 자체를 외부에 줄 필요 없음.
* Hot ring buffer 가 자기 chain 을 따로 가지면 power-fail 시 합칠 때 일관성 검증 추가 필요. 그냥 ring buffer 는 *Tier 2 에 곧 flush 될 in-flight* 로만 취급.

### 데이터 flow

```
PreToolUse hook
   ↓
build_atv()
   ↓
Tier 3 ring buffer (in-flight, ~ms)
   ↓ (synchronous flush at step360)
Tier 2 partition (warm, sign + chain advance + AES-GCM journal)
   ↓ (async tier-down, periodic)
Tier 1 cold archive (per-tenant aggregation)
   ↓ (read-only, on-demand)
PPA cross-tenant aggregation view
```

각 tier 는 **다음 tier 로의 transition 시점에 그 tier 의 책임을 다한다** — Tier 3 는 ATMU 2PC 로 commit/compensate 결정, Tier 2 는 chain integrity, Tier 1 은 retention/archive policy.

### Cardinality math

```
1 user × 1 session × 50 tool calls × 6 events                  =     300 ATV records
                                                                       per session

활성 hospital × 4 agents × 100 sessions/day × 50 calls × 6 events ≈   120,000 ATV records / day
                                                                       per tenant

1 ATV ≈ 8,320 bytes tensor + 250 B header + 64 B sig            ≈      8.6 KB
                                                                       per record

하루 저장: 120,000 × 8.6 KB                                       ≈      1 GB / day / tenant
1년 retention: × 365                                             ≈    370 GB / year / tenant
```

→ Tier 2 partition 은 NVMe 1 ~ 4 TB 면 1 tenant 5 ~ 10 년 retention 충분. Tier 1 cold archive 는 S3/Glacier-class 면 충분. **1 sLLM-as-a-firewall 으로 SSD 가 부족한 시점은 deployment 단위에서 계산해 봐야 함** (수백 tenant 동시 운영 시).

---

## 6. 5 blind spots + 완화 설계

### B-1. "Session" 의 정의

문제: 장기 실행 agent (`MedRAG-CKD` 가 6개월 deploy) 의 session 경계 모호.

대안 비교:

| 옵션 | 정의 | 장점 | 단점 |
|------|------|------|------|
| (a) per prompt | 매 user prompt = 1 session | 단순 | long-horizon belief drift 못 잡음 |
| (b) per process | agent 프로세스 살아있는 동안 = 1 session | belief drift 잡힘 | session 단위 너무 큼 |
| **(c) per HAM commit** ★ | HAM L3+L4 memory commit boundary = 1 session | 의미적 명확 + memory rotation 자연스러움 | HAM 미구현 시 대체 정의 필요 |

→ 권장: **(c) HAM commit boundary**. HAM 미구현 deployment 에서는 *연속 24시간 무활동* 을 fallback session 종료 신호로.

### B-2. Multi-AID call tree

문제: orchestrator A 가 sub-agent B 를 tool 로 호출 시 ATV 가 어디 chain 에 속하나?

해법: **양쪽 chain 에 각각 record + cross-link**.

```
A's chain                         B's chain
  ATV_A_4 ────────────────────►  ATV_B_0 (session_init)
  (action_txn_id=T1,                (parent_atv_hash=ATV_A_4.hash)
   delegation_to=B's session_id)    
```

→ A 입장: "step 4 에서 B 를 호출했다" — chain forward
→ B 입장: "B's session 의 root 는 A's step 4 가 만든 것" — chain backward

Forensic replay 시 어느 record 에서 시작해도 cross-link 따라 양쪽 다 reachable.

### B-3. 생성 단위 vs. 저장 계층 명시 분리

이미 §2 vs §5 로 분리했지만, **문서/코드/영업 자료에서 둘을 섞어 쓰면 안 됨**:

| 잘못된 표현 | 올바른 표현 |
|------------|-------------|
| "Tenant 당 1개 ATV 가 할당된다" | "Tenant 는 ATV 의 namespace; record 는 step 단위 생성" |
| "Container 는 ATV boundary" | "Container 는 runtime attestation field; chain boundary 는 (instance, session)" |
| "3-tier 가 ATV unit 이다" | "3-tier 는 storage hierarchy; ATV unit 은 per-step" |

### B-4. Identifier naming alignment

Patent claim 의 단어와 code 의 변수명이 1:1 정렬돼야 prosecution 시 deficiency 안 잡힘.

| Patent term | 현재 code | 본 PR 에서 정렬 |
|-------------|-----------|----------------|
| `agent_id` (logical) | ❌ 없음 | **추가** (default = aid) |
| `agent_instance_id` | `aid` | **추가** (alias of aid) |
| `session_id` | `trace_id` (OTel) | **추가** (alias of trace_id) |
| `runtime_context_id` | ❌ (node_id + pod_id 분리) | **추가** (consolidated) |
| `step_seq_no` | external | **추가** as field |
| `action_txn_id` | `span_id` | **추가** (alias of span_id) |
| `parent_atv_hash` | `prev_hash` (linear) | **추가** (tree-shaped, optional) |

→ 본 PR (#100) 에서 모두 추가, back-compat 유지 (legacy 필드 그대로 살아있음).

### B-5. Solo Free degenerate mode

문제: 위 5층 (tenant/deployment/runtime/agent/session) 모델이 1-user Mac mini 환경에서 over-engineered 로 보임.

해법: **Solo Free 에서 5층이 모두 collapse, 단일 implicit value 로 fill**.

```
Solo Free 의 implicit identifier:
  tenant_id            = "solo-free-local"
  deployment_id        = $(hostname)
  runtime_context_id   = "local-process"
  agent_id             = "claude-code"
  agent_instance_id    = $(uuidgen)  per Claude Code session
  session_id           = $(uuidgen)  per workspace open
  step_seq_no          = monotonic counter
```

이러면 Solo Free 도 동일 schema 로 작동, audit.jsonl 도 multi-tenant deployment 에 그대로 import 가능 (forensic + sales pilot 용).

---

## 7. Solo Free degenerate mode (확장)

§6 B-5 의 mapping 을 그대로 적용. 추가로:

* **Audit chain**: Solo Free 도 `(agent_instance_id, session_id)` 로 묶지만 단일 인스턴스라 사실상 *flat chain*. v3.x 까지는 단일 `~/.aegis/audit.jsonl` 그대로.
* **Tier 1/2/3 collapse**: 모두 single Python process + 단일 jsonl 파일. *spec 위반 없이* — generation/chain semantics 는 동일.
* **Sidecar 로 promotion 시**: Solo Free 의 audit.jsonl 을 그대로 import 가능 (header 가 multi-tenant schema 이미 따름).

---

## 8. Patent 정렬 — dependent claim draft

본 분석으로 도출된 dependent claim, 다음 patent prosecution round 에 추가 권고:

> **The system of claim 1**, wherein each Agent Telemetry Vector is generated for an atomic execution step or proposed tool-invocation transaction of a particular agent instance within an agent session, and wherein said Agent Telemetry Vector includes identifiers for a tenant, deployment, runtime context, container or hardware attestation boundary, logical agent, agent instance, session, step sequence number, and tool-invocation transaction.

추가 dependent claims:

> **The system of claim X**, wherein multiple agent instances executing within a common runtime context produce distinct Agent Telemetry Vector chains, and wherein a single agent instance migrating across or invoking workers in multiple runtime contexts maintains a logically continuous chain through parent-hash or delegation-hash linkage.

> **The system of claim X**, wherein an Agent Telemetry Vector is generated at each of: (i) session initialization, (ii) external instruction ingestion, (iii) proposed tool invocation, (iv) tool result ingestion, (v) cross-agent reviewer attestation, and (vi) baseline mutation or re-attestation.

> **The method of claim Y**, comprising emitting a tenant-keyed cryptographic chain in a Tier 2 agent partition, exposing only a Tier 1 aggregated view to cross-tenant analytics, and maintaining a Tier 3 hot ring buffer with power-fail-safe flush to Tier 2 prior to chain commitment.

---

## 9. Roadmap — gap 별 PR 매핑

§4 + §5 + §6 의 gap 을 PR 트랙으로 매핑:

| Gap | 본 PR (#100) | 후속 PR 트랙 | 우선순위 |
|-----|-------------|------------|---------|
| ATVHeader 식별자 정렬 (B-4) | ✅ 본 PR | — | high — patent prosecution 직결 |
| Event 1 — session_init ATV emit | — | v3.2 / `feat/atv-session-init` | medium |
| Event 2 — external_ingest ATV emit | — | v3.2 / `feat/atv-external-ingest` | **high** — belief drift 추적 |
| Event 4 — tool_result ATV expand | — | v3.2 / `feat/atv-tool-result` | medium (PostToolUse 확장) |
| Event 5 — reviewer attest (RAAM) | — | v4.0 / `feat/raam` | medium-low (multi-agent 시 필수) |
| Event 6 — re_attest ATV | — | v3.2 / `feat/atv-reattest` | low |
| Tier 1 namespace (CXL) | — | v4.0 hardware track | low (Sidecar 부터 필요) |
| Tier 2 per-AID partition | — | v3.3 / `feat/audit-aid-partition` | medium |
| Tier 3 ring buffer | — | v4.0 hardware track | low |
| Multi-AID call tree (B-2) | ⚠️ partial (parent_atv_hash 필드 추가) | 채움 PR `feat/atv-call-tree` | medium |
| Session 정의 (B-1) | — | HAM commit boundary 결정 후 | depends on M16 |

→ 본 PR 은 **B-4 만 완전 해결, 나머지는 명시화**. 향후 PR 의 약속이 문서화됐으니 외부 due diligence 에서 *aspirational only* 가 아닌 *roadmapped* 로 평가받음.

---

## 10. 부록 — full ATVHeader v2 schema (post PR #100)

```python
class ATVHeader(BaseModel):
    # ── legacy fields (v1, kept for back-compat) ───────────────────
    trace_id: str                          # OTel trace
    span_id: str                           # OTel span
    parent_span_id: str | None = None
    tenant_id: str                         # 고객 / 조직
    aid: str                               # legacy: AID == agent_instance_id
    ats: str = "ATV-2080-v1"               # legacy alias of schema_version
    schema_version: str = "ATV-2080-v1"
    timestamp_ns: int                      # Unix ns
    node_id: str | None = None
    pod_id: str | None = None
    tier_profile: Literal["T2", "T3"] = "T2"
    cost_attestation_profile: Literal["software", "hardware", "both"] = "software"
    model_hash: str | None = None
    burn_in_id: str | None = None
    atv_hash: str | None = None

    # ── PR #100 patent-aligned identifiers ─────────────────────────
    agent_id: str | None = None            # logical agent role/principal
    agent_instance_id: str | None = None   # stateful execution context
    session_id: str | None = None          # explicit session anchor
    runtime_context_id: str | None = None  # consolidated container/TEE/CSD identity
    step_seq_no: int = 0                   # turn counter within session
    action_txn_id: str | None = None       # alias of span_id, patent-named
    parent_atv_hash: str | None = None     # tree-shaped chain (call tree)
    deployment_id: str | None = None       # consolidated node_id-style fingerprint
    policy_id: str | None = None           # active firewall policy fingerprint
    attestation_key_id: str | None = None  # which Ed25519 key signed

    # When patent-aligned fields are unset, model_validator fills them
    # from the legacy fields so existing code paths remain valid.
```

→ **Back-compat invariant**: legacy 필드만 채우는 v1 caller 가 v2 schema 위에서 그대로 동작. 새 PR 들은 patent-aligned 필드를 명시적으로 채움.

---

**문서 변경 시 follow-up 필요**: 본 문서가 변경되면 (1) [whitepaper](../WHITEPAPER.md), (2) patent draft, (3) [investor pitch deck](build/PITCH_DECK.pdf) 모두 영향. 변경 시 이 3 곳을 같이 update 하는 것을 잊지 말 것.
