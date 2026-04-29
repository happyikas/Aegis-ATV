# AegisData 특허 보강 명세 (Provisional Supplement v3)

**버전:** v3.6 (2026-04-28)
**대상 출원:** US Provisional Patent `ATV_v7_10` (40 claims)
**보강 범위:** ATV-2080 의 **트러스트 면**(기존 Claim 1–40)에서 **퍼포먼스 면**으로의 확장
**구현 참조:** `src/aegis/performance/`, `src/aegis/judge/unified_head.py`,
`integrations/{mlx_lm,llama_cpp,vllm}/`, `docs/VLLM_INTEGRATION_DESIGN.md`

---

## 0. 한 줄 요약

> 기존 ATV-2080 텐서가 **AI 에이전트 도구 호출의 신뢰 검증**을 위해 설계되었으나,
> 본 보강은 동일 텐서를 입력으로 **LLM 추론 런타임의 KV cache prefetch /
> scheduling / memory placement** 를 하나의 결정론적 sub-millisecond 함수로
> 자문(advisory)하는 메커니즘을 청구한다. **트러스트 면과 퍼포먼스 면이
> 동일 ATV 텐서와 동일 attribution-head 아키텍처를 공유**한다는 것이
> 핵심 차별점이다.

---

## 1. 배경 (기존 청구의 한계)

기존 출원 (`ATV_v7_10`) 의 청구는 다음에 한정되어 있다:

| 청구 번호 | 영역 | 출력 |
|---|---|---|
| Claim 1, 2 | ATV-2080 schema | 30 subfield × 1880 SW + 200 HW = 2080-D |
| Claim 6, 7 | Sub-field encoder | 4 family (TEXT-EMBED / HASH-EXPAND / FEATURE-EXTRACT / ZERO) |
| Claim 8 | M13 attribution head | (verdict, confidence, 30-key attribution) |
| Claim 9 | Audit replay | model_hash 기반 deterministic re-run |
| Claim 26, 27, 30 | HW/SW double-check | cost-divergence escalation |
| Claim 34 | Cost attestation key | telemetry key 와 분리된 별도 서명 슬롯 |

본 청구의 **출력은 모두 안전성 검증 (3-class verdict)** 에 한정된다.

**한계점:**
1. 동일 텐서가 가진 perf-relevant 신호 (`cache_hit_rate`,
   `context_utilization_ratio`, `task_progress_score`,
   `composite_novelty`, `prompt_structure`, `inter_agent_graph`,
   `memory_provenance`) 가 **활용되지 않은 채 폐기**되고 있다.
2. LLM 서빙 런타임 (vLLM, MLX-LM, llama.cpp, SGLang) 은 자체 LRU /
   휴리스틱에 의존하여 KV cache 를 관리, **multi-agent 환경에서 발생하는
   sub-task 단위의 hot/cold 패턴을 인식하지 못한다.**
3. AI 에이전트별 cohort 정보 (동일 task phase, 유사 prompt 구조) 가
   **scheduling 으로 흘러가지 못해**, 같은 cohort 의 요청이 분산 배치되어
   batching 이득이 사라진다.

---

## 2. 보강 청구의 출발점 (Architectural Insight)

**핵심 통찰:** ATV-2080 의 30 subfield 중 **13 개가 perf-relevant 시그널을
담고 있다**:

| Subfield | 슬롯 | Perf 의미 |
|---|---|---|
| `cost_efficiency_metrics` | 16 | `cache_hit_rate` (s-10), `context_utilization_ratio` (s-11), `cumulative_tokens` (s-4), `task_progress_score` (s-15) |
| `novelty_score` | 4 | `composite_novelty` (idx 3) — speculative decode 적합도 |
| `prompt_structure` | 16 | code-block presence, length-norm — speculative & batching |
| `action_history` | 640 | 같은 history 패턴 = 같은 KV layout |
| `agent_state_embedding` | 768 | cohort key 의 안정 부분 |
| `inter_agent_graph` | 128 | cross-agent shared prefix |
| `memory_provenance` | 64 | hash-derived KV segment id |
| `action_blast_radius` | 16 | preempt safety 신호 |
| `tool_arg_inspection` | 32 | destructive verb / fs write — preempt 위험 |
| `human_oversight_state` | 8 | operator presence → priority class |
| `aid_ats_scalars` | 8 | T3 deployment 여부 → CSD 티어 가용성 |
| `qom_scores` | 16 | progress freshness/relevance 종합 |
| `mcp_trust_signals` | 12 | tool desc churn → cache invalidation 신호 |

따라서 같은 텐서를 입력으로 **다른 헤드를 추가**하면 별도 모델 코드 수정 없이
런타임에 perf hint 를 흘릴 수 있다.

---

## 3. 보강 청구 (Claims Extension)

### 3.1 Claim 41 — ATV-기반 KV cache 자문 헤드

> **`Claim 41`**: 청구항 1 의 ATV-2080 텐서를 입력으로 하여
> ``(prefetch_segment_ids, evict_candidates, residency_class,
> batch_key, speculative_decode, advisor_hash)`` 를 출력하는
> **결정론적 sub-millisecond 자문 함수**를 갖는 시스템으로서, 상기
> ``residency_class`` 는 ``cost_efficiency_metrics`` 의 슬롯
> ``s-10/s-11/s-15`` 와 ``novelty_score`` 의 ``composite_novelty``
> 로부터 hot|warm|cold 의 3-class 로 결정되고, 상기 ``batch_key`` 는
> ``agent_state_embedding`` + ``action_blast_radius`` 의 양자화된
> SHA3-256 해시로부터 도출되며, 상기 ``advisor_hash`` 는 자문 함수의
> 코드 버전 식별자의 SHA3-256 으로 정의되어 audit replay 가
> 가능한 시스템.

**구현 참조:** [src/aegis/performance/kv_cache_advisor.py](../src/aegis/performance/kv_cache_advisor.py)

### 3.2 Claim 42 — Closed-loop perf attestation

> **`Claim 42`**: 청구항 41 의 시스템에 있어, 상기 LLM 서빙 런타임이
> 측정한 ``cache_hit_rate``, ``context_utilization_ratio``,
> ``tokens_per_second``, ``runtime_latency_ms``, ``memory_peak_bytes``
> 를 (tenant_id, aid) 키 별 **EWMA 저장소**에 누적하고, 다음 turn 의
> ATV 빌드 시 호스트가 명시적으로 채우지 않은 cost band 슬롯을
> 상기 EWMA 값으로 backfill 하되, **호스트 명시 값은 결코 덮어쓰지
> 않음으로써** 호스트 우선권을 보장하는 시스템. 상기 EWMA 가
> 청구항 34 의 cost-attestation key 로 서명되는 경우, 자문은
> **서명된 측정치 기반의 폐쇄 루프 자문 (signed closed-loop
> advisory)** 이 된다.

**구현 참조:** [src/aegis/performance/feedback.py](../src/aegis/performance/feedback.py),
[src/aegis/api/tool_outcome.py](../src/aegis/api/tool_outcome.py)

### 3.3 Claim 43 — Scheduling 자문 헤드

> **`Claim 43`**: 청구항 1 의 ATV 를 입력으로
> ``(priority_class, preempt_safe, max_concurrent_in_cohort,
> deadline_ms, advisor_hash)`` 를 출력하는 결정론적 sub-millisecond
> scheduling 자문 함수로서, ``priority_class`` 는
> ``human_oversight_state`` 의 ``operator_present``,
> ``action_blast_radius`` 의 ``blast_radius_norm``,
> ``novelty_score`` 의 ``composite_novelty`` 로부터
> interactive|batch|low 의 3-class 로 결정되고,
> ``preempt_safe`` 는 ``tool_arg_inspection`` 의 ``destructive_verb``
> + ``filesystem_write`` 슬롯의 부재로 결정되는 시스템.

**구현 참조:** [src/aegis/performance/scheduling_advisor.py](../src/aegis/performance/scheduling_advisor.py)

### 3.4 Claim 44 — Memory placement 자문 헤드

> **`Claim 44`**: 청구항 1 의 ATV 를 입력으로 ``(layer_residency_plan,
> kv_quantisation_dtype, prefetch_window_tokens, swap_threshold_bytes,
> advisor_hash)`` 를 출력하는 결정론적 자문 함수로서, 상기
> ``layer_residency_plan`` 은 [layer_index → tier] 의 사상으로 표현
> 되고, ``tier`` 는 hbm|cpu|csd 중 선택되며, ``aid_ats_scalars`` 의
> T3-flag 가 set 된 경우에 한해 csd tier 가 후보가 되어 청구항 26
> 의 cost-attestation profile 과 정렬되는 시스템.

**구현 참조:** [src/aegis/performance/placement_advisor.py](../src/aegis/performance/placement_advisor.py)

### 3.5 Claim 45 — Unified attribution head

> **`Claim 45`**: 청구항 8 의 M13 attribution head 를 확장하여,
> 단일 ATV 입력 통과로 (a) 청구항 8 의 verdict + 30-key attribution,
> (b) 청구항 41 의 KV cache 자문, (c) 청구항 43 의 scheduling 자문,
> (d) 청구항 44 의 placement 자문 의 **4 종 출력을 동시에**
> 생성하고, 상기 4 출력의 advisor_hash 들을 정렬-결합한
> SHA3-256 을 ``unified_hash`` 로 발행하여, audit replay 시
> 4 헤드 중 **하나라도 버전이 다르면 unified_hash 가 달라져
> 검출 가능한** 시스템.

**구현 참조:** [src/aegis/judge/unified_head.py](../src/aegis/judge/unified_head.py)

### 3.6 Claim 46 — Advisor-as-hint protocol

> **`Claim 46`**: 청구항 41–45 의 자문 출력은 **권고 (advisory)** 로
> 만 정의되고, LLM 서빙 런타임은 자문을 (a) 적용, (b) 부분 적용,
> (c) 무시 중 임의를 선택할 수 있으며, 자문이 5 ms 이내에
> 도착하지 않거나 ``confidence < threshold`` 인 경우 런타임은
> 자체 휴리스틱으로 **graceful fallback** 한다. 본 advisory-only
> 프로토콜로 인해 LLM 모델 코드의 수정이 요구되지 않으며, 본
> 발명은 **모델 외부의 메모리/스케줄러 레이어** 에 한정 적용된다.

**구현 참조:** [docs/VLLM_INTEGRATION_DESIGN.md](VLLM_INTEGRATION_DESIGN.md),
[integrations/vllm/__init__.py](../integrations/vllm/__init__.py)

### 3.7 Claim 47 — Cross-tenant cache federation (선택적)

> **`Claim 47`**: 청구항 41 의 ``batch_key`` 는 ``agent_state_embedding``
> + ``action_blast_radius`` 만 의존하므로, **여러 tenant 가 동일
> 모델/동일 task phase 에 있을 때 동일 batch_key 를 공유**할 수 있고,
> 이 경우 청구항 34 의 cost-attestation key 를 통해 **tenant 간 KV
> segment 의 cross-rental** 이 권리행사로 가능하며, 자문은 해당
> federation 의 멤버십 hint 로 동작하는 시스템.

**Note:** Claim 47 은 옵션 — federation 기능 자체는 v3.x 에 미구현,
v4.x 의 milestone 으로 예약.

### 3.8 Claim 48 — Context window 자문 헤드 (v3.7 신규)

> **`Claim 48`**: 청구항 1 의 ATV 시퀀스 (현재 turn 의 ATV +
> 과거 turn 의 ATV 리스트 + per-turn token cost) 를 입력으로
> ``(keep_verbatim_turn_ids, summarize_turn_ids,
> replace_with_atv_turn_ids, drop_turn_ids,
> expected_token_savings, advisor_hash)`` 를 출력하는 결정론적
> sub-millisecond 자문 함수로서, **per-turn relevance score** 는
> 현재 ATV 의 ``agent_state_embedding`` 과 과거 ATV 의 cosine
> similarity (가중치 0.45), ``cost_efficiency_metrics`` 의
> ``task_progress_score`` (s-15) 매치 (0.20), ``novelty_score`` 의
> ``composite_novelty`` 근접도 (0.10), 지수 감쇠
> (half-life=8 turns) recency (0.25) 의 **가중 합** 으로 계산되며,
> 상기 score 와 token_budget 제약으로부터 **그리디 ROI 정렬** 을
> 통해 turn 단위 결정이 도출되는 시스템.

**구현 참조:** [src/aegis/performance/context_advisor.py](../src/aegis/performance/context_advisor.py)

**핵심 차별점:**
- **트러스트 검증 + KV cache 자문 + scheduling 자문 + placement 자문 + context window 자문** 이 모두 동일 ATV-2080 위에 정의
- LLM 자체 (Anthropic / OpenAI / 로컬) 의 자동 compaction 과 다르게,
  **task-phase-aware** 결정 — 현재 phase 와 무관한 과거 phase 우선 압축
- token_budget 제약 하 **결정론적 그리디 최적화** → audit replay 가능

### 3.9 Claim 49 — Subfield-selective ATV 압축 (v3.x 예약)

> **`Claim 49`**: 청구항 48 의 시스템에 있어, 과거 turn 의 ATV 들을
> turn-to-turn delta 로 표현하여, 변하지 않은 subfield 는 reference +
> 차이만 저장하는 **subfield-selective ATV diff 압축** 을 적용함으로써
> 컨텍스트 메모리 사용량을 추가로 절감하는 시스템.

**Note:** Claim 49 의 reference implementation 은 v3.8/v4.x 로 예약.

### 3.10 Claim 50 — Unified head 5-output 통합 (v3.x 예약)

> **`Claim 50`**: 청구항 45 의 unified head 를 확장하여,
> (a) verdict, (b) KV cache 자문, (c) scheduling 자문,
> (d) placement 자문, (e) **청구항 48 의 context window 자문**
> 의 5 출력을 single ATV 입력 통과로 동시 생성하고,
> 5 출력 의 advisor_hash 를 정렬-결합한 SHA3-256 을 신규
> ``unified_hash_v2`` 로 발행하는 시스템.

**Note:** Claim 50 의 reference implementation 은 v3.8 의 milestone.
v3.7 에서는 context_advisor 가 standalone 으로 동작 — unified head 와
endpoint 가 분리되어 있어, 호출자가 필요한 자문만 선택적으로 호출 가능.

### 3.11 Claim 51 — Group-commit 감사 체인 (v3.8 신규)

> **`Claim 51`**: 청구항 1 의 audit chain 의 비휘발성 저장에 있어,
> 다수 ATV 의 ``append`` 호출을 in-memory 큐에 적재한 후 ``batch_size``
> 도달 또는 ``interval_ms`` 경과 시 단일 ``open() / write_all() /
> fsync() / close()`` cycle 로 일괄 처리하되, 각 ``append`` 호출자가
> **자신이 속한 batch 의 fsync 가 성공한 후에만 반환** 하는 시스템.
> 본 시스템은 N 호출당 fsync 1 회로 throughput 을 약 N 배 증가시키면서도
> 호출자 입장에서의 durability semantic (RPO=0) 을 보존하는 시스템.

**구현 참조:** [src/aegis/audit/group_commit.py](../src/aegis/audit/group_commit.py),
[src/aegis/audit/encrypted_journal.py](../src/aegis/audit/encrypted_journal.py)

**핵심 차별점:**
- 기존 sync-per-append 와 비교, throughput N× (e.g. 100 batch → 100×)
- 각 호출자가 fsync 완료 후 return → caller-side durability 동일
- On-disk format 변경 없음 → plain `EncryptedJournal` 와 cross-compatible
- Fsync 실패 시 batch 의 모든 caller 가 동일 exception (atomicity at batch
  granularity)

### 3.12 Claim 52 — 계층 보존 (Tiered Archive) 감사 체인 (v3.9 신규)

> **`Claim 52`**: 청구항 1 의 audit chain 의 비휘발성 저장에 있어,
> 다음 3 계층으로 감사 데이터를 분산 저장하는 시스템:
> (a) **Hot tier**: 활성 NVMe / SSD 의 live 파일 (≤1 ms write).
> (b) **Warm tier**: 회전된 segment 파일 (보존 정책 N segments 까지).
> (c) **Cold tier**: 객체 저장소 (S3 / GCS / Azure Blob 또는 NFS) 에
>     기록된 backend-specific identifier 로 추적되는 segment.
> 회전 트리거는 ``rotate_bytes`` 도달 또는 ``rotate_seconds`` 경과
> 중 빠른 쪽이며, 회전된 segment 는 **idempotent archive backend** 를
> 통해 cold tier 로 이동되고, hot tier 는 ``hot_retention_segments``
> 만 유지하는 시스템.

**구현 참조:** [src/aegis/audit/tiered_archive.py](../src/aegis/audit/tiered_archive.py)

**핵심 차별점:**
- 모든 ATV 가 결국 cold tier (11-자리 durability — S3 99.999999999 %) 도달
- Hot/Warm tier 비용 절감: 활성 segment 만 SSD 에 보유, 90 % 이상은 archive
- 암호화 + commitment chain 변경 없이 cross-tier replay 가능
- 청구항 47 의 cross-tenant federation 의 기반 인프라 (cold segment 가
  cohort tag 와 함께 저장됨)

### 3.13 Claim 53 — Persistent perf feedback snapshot (v3.8 신규)

> **`Claim 53`**: 청구항 42 의 EWMA 저장소를 비휘발성으로 보존하기 위한
> 시스템에 있어, ``min(interval_sec, updates_per_snapshot)`` 트리거로
> 전체 EWMA state 를 SQLite 에 (tenant_id, aid) 기준 단일 row 로
> persist 하고, restart 시 ``load_into_store()`` 가 prior state 를
> 복원하여 advisor 의 cold-start warm-up 비용을 제거하는 시스템. RPO 는
> ``interval_sec`` 으로 한정되며 (default 30 sec), advisor 는
> advisory-only 이므로 RPO 손실 시 graceful degradation 으로 native
> heuristic 으로 fallback 한다.

**구현 참조:** [src/aegis/performance/feedback_snapshot.py](../src/aegis/performance/feedback_snapshot.py)

### 3.14 Claim 54 — 정기 audit patrol (v4.0 신규)

> **`Claim 54`**: 청구항 1 의 audit chain 의 비휘발성 저장에 있어,
> 백그라운드 daemon 이 (a) **Merkle chain 재검증** (audit DB), (b)
> **Ed25519 서명 재검증** (audit DB + cost ledger), (c) **AES-GCM
> auth tag 재검증** (encrypted journal), (d) **SHA3 commitment
> 재계산** (모든 store), (e) **cross-store consistency 매칭** (SQLite ↔
> JSONL ↔ encrypted journal), (f) **monotonic sequence gap 감지**
> (ATMU = Agent Telemetry Management Unit, intent_log) 의 6 가지
> 검증을, 각각 **독립적 cadence** (full=6h / sample=1h /
> sequence=5min / consistency=1h / cold=24h) 로 수행하고, 발견 시
> (BLOCK / alert / replay) 중 하나의 자동 대응을 trigger 하는
> patrol 시스템.

**구현 참조:** [src/aegis/audit/patrol.py](../src/aegis/audit/patrol.py),
[src/aegis/api/audit_patrol.py](../src/aegis/api/audit_patrol.py)

**핵심 차별점:**
- 기존 무결성 검증은 **on-read / on-demand** (verify-audit CLI 또는 read 시점)
  → bit-rot / silent corruption 이 영원히 잠복 가능. patrol 이 **proactive 발견**
- Cold tier (v3.9) 와 결합: 1 년간 안 읽힌 archived segment 도 24h 마다
  sample 검증
- 6 가지 검증이 **독립 cadence** 로 동작 — sequence gap 은 5min 마다, full
  re-sweep 은 6h 마다 (운영 비용 vs 검출 지연 trade-off)
- 발견된 finding 의 severity 가 critical 이면 사이드카가 **즉시 BLOCK
  모드** 로 전환 가능 (v4.x extension)
- T3 hardware 시 patrol report 자체를 cost-attestation key (Claim 34) 로
  서명 → "patrol 가 거짓말 안 했다" 도 증명

**6 검증의 매칭표:**

| 검증 | 대상 store | 검출 가능 공격 |
|---|---|---|
| Merkle chain | audit DB + cost ledger | record 삭제, 재배열 |
| Ed25519 signature | audit DB + cost ledger | active tampering |
| AES-GCM auth tag | encrypted journal | cipher tamper, key compromise |
| SHA3 commitment | 모든 store | bit-rot, silent corruption |
| Cross-store consistency | SQLite ↔ JSONL ↔ journal | partial write, malicious deletion |
| Sequence gap | ATMU intent_log | record skipping, reordering |

### 3.15 Claim 55 — Multi-source HW telemetry aggregator (v4.1 신규)

> **`Claim 55`**: 청구항 1 의 ATV HW band 200-D 를 채우기 위해, 다음
> **8 source collector** 를 단일 :class:`CollectorAggregator` 에서
> 호출하는 시스템:
> (a) CPU PMU (perf_event / `/proc/stat`),
> (b) DRAM ECC (Linux EDAC subsystem),
> (c) IOMMU (`/sys/class/iommu/`, `/sys/kernel/iommu_groups/`),
> (d) NIC counters (`/proc/net/dev`),
> (e) NVIDIA GPU (NVML / DCGM, optional),
> (f) BMC (Redfish HTTP, out-of-band),
> (g) TEE attestation quote (TDX / SEV-SNP / CCA, mock-able for T2),
> (h) **Aegis-FPGA** (M21+ 커스텀 silicon, mock-able).
> 각 collector 는 :class:`HWCollector` Protocol 을 구현하여
> ``is_available()`` 으로 graceful degradation 을 보장하고,
> aggregator 는 **collector 우선순위표** 에 따라 동일 slot 의 다중
> source 충돌을 결정론적으로 해소하며, 미커버 slot 은 v2.3
> simulator 의 honest baseline 으로 채워 ATV HW band 가 부분
> zero-fill 로 노이즈가 끼는 것을 방지하는 시스템.

**구현 참조:** [src/aegis/hw_telemetry/collectors/](../src/aegis/hw_telemetry/collectors/)

**핵심 차별점:**
- 기존 v2.3 simulator 만 → 8 source 의 **실 HW 데이터 통합**
- 각 collector 가 **graceful degradation** — 인터페이스 부재 시 자동 skip
- T2 환경에서 약 **70 % 의 HW band slot** 이 실 데이터 (PMU, EDAC, NVML,
  ethtool, IOMMU 가 동작; TEE / Aegis-FPGA 는 mock 유지)
- T3 silicon 도착 시 mock collector 만 swap → aggregator + firewall 변경 없음
- Buggy collector 가 raise 해도 aggregator 가 swallow — never kill the call
- 동일 slot 의 multi-source 충돌은 **frozen priority order** 로 해소
  (audit replay 시 결정론적 재현 보장)

**Collector 우선순위 (frozen, advisor_hash 처럼 patent 청구의 일부):**

```
flops_observed:           NVML > PMU > simulator
gpu_utilization:          NVML > PMU > simulator
hbm_bytes_observed:       NVML > simulator
hbm_utilization:          NVML > simulator
thermal_celsius_p95:      NVML > BMC > simulator
network_bytes_in/out:     ethtool > simulator
dma_fanout:               IOMMU > simulator
ecc_correctable/uncorr:   EDAC > simulator
iommu_tag_violations:     AegisFPGA > IOMMU > simulator
hypervisor_ring_violations: TEE quote > simulator
watchdog_strikes:         TEE quote > simulator
dram_access_pattern_entropy: AegisFPGA > simulator
```

---

## 4. 본 보강의 차별점 (Why This Is Novel)

### 4.1 트러스트와 퍼포먼스의 통합 텐서

| 관점 | 기존 LLM 시스템 | AegisData v3 |
|---|---|---|
| 트러스트 검증 | Constitutional AI / 별도 firewall | ATV-2080 + M13 head |
| 퍼포먼스 최적화 | LRU / vLLM PagedAttention 자체 | ATV-2080 + 자문 헤드 |
| 입력 텐서 | **분리** | **통일** (동일 2080-D) |
| 학습 | 독립 | M13 weights 학습 시 양면 동시 신호 사용 가능 (v4.x) |

### 4.2 Advisory-only 프로토콜의 함의

- vLLM / MLX-LM / llama.cpp 중 **하나도 fork 하지 않는다**.
- 모델 가중치 / inference 코드 일체 변경 없음.
- 자문 endpoint (HTTP, ≤5 ms p99) 가 모든 결합점.
- Aegis 가 unreachable 이어도 런타임은 native 휴리스틱으로 동작.

### 4.3 Closed-loop attestation

- 호스트 자기보고 vs 런타임 측정 의 **이중 신호**.
- T3 hardware (M19+) 와 결합 시 측정치 자체가 cost-attestation key 로
  서명 → audit-grade perf telemetry.
- 청구항 26/27 의 HW/SW double-check 와 같은 패턴 적용.

### 4.4 Unified hash 의 audit 가치

- 4 헤드 중 어느 하나라도 버전 변경 → ``unified_hash`` 변경.
- ``aegis verify-audit`` 가 트러스트 + 퍼포먼스 자문 전체를 한 번에 검증.
- 규제 측에서 "누가 어떤 perf 결정을 했는가" 를 결정론적으로 재현 가능.

---

## 5. 실시례 (Reference Implementation)

### 5.1 v3.1 KV cache advisor

```
src/aegis/performance/kv_cache_advisor.py     ←  Claim 41
src/aegis/api/advisory.py                     ←  POST /advisory/kv_cache
demo/kv_cache_advisor.py                      ←  5-scenario demo
tests/unit/test_kv_cache_advisor.py           ←  14 unit tests (PASS)
```

**측정값** (M3 Mac, 2026-04):
- 평균 latency: 0.011 ms (p50), 0.035 ms (p99)
- Bit-determinism: 동일 ATV 입력에서 100 % 동일 출력
- Pure function: 외부 I/O 없음, lock 없음

### 5.2 v3.2 Closed-loop feedback

```
src/aegis/performance/feedback.py             ←  Claim 42
src/aegis/api/tool_outcome.py                 ←  /tool-outcome 확장
src/aegis/api/{advisory,evaluate}.py          ←  backfill 적용
tests/unit/test_perf_feedback.py              ←  13 unit tests (PASS)
```

**EWMA 파라미터:** α=0.30 (recent 30 %, history 70 %).
호스트 명시 값은 절대 덮어쓰지 않음 (test_host_supplied_value_not_overwritten 으로 검증).

### 5.3 v3.4 Scheduling + Placement advisors

```
src/aegis/performance/scheduling_advisor.py   ←  Claim 43
src/aegis/performance/placement_advisor.py    ←  Claim 44
src/aegis/api/advisory.py                     ←  /advisory/{scheduling,placement,all}
tests/unit/test_scheduling_placement.py       ←  18 unit tests (PASS)
```

### 5.4 v3.5 vLLM integration

```
integrations/vllm/__init__.py                 ←  Claim 46 reference shim
docs/VLLM_INTEGRATION_DESIGN.md               ←  3 plug-point design
tests/unit/test_runtime_adapters.py           ←  10 unit tests (PASS, 3 vLLM)
```

### 5.5 v3.6 Unified head

```
src/aegis/judge/unified_head.py               ←  Claim 45
src/aegis/api/advisory.py                     ←  POST /advisory/unified
tests/unit/test_unified_head.py               ←  8 unit tests (PASS)
```

**unified_hash 안정성:** 동일 4 헤드 버전 → 동일 hash, 입력 ATV 무관
(test_unified_hash_stable_across_calls 로 검증).

**트러스트 경로 동등성:** UnifiedHead.evaluate_unified() 의 verdict
경로는 v2.5 AttributionHead 와 bit-identical
(test_unified_verdict_path_matches_attribution_head 로 검증).

### 5.6 v3.7 Context window advisor

```
src/aegis/performance/context_advisor.py      ←  Claim 48
src/aegis/api/advisory.py                     ←  POST /advisory/context
demo/context_advisor.py                       ←  12-turn 3-budget demo
tests/unit/test_context_advisor.py            ←  14 unit tests (PASS)
```

**측정값** (M3 Mac, 2026-04):
- 50-turn history: 평균 latency 0.087 ms (실측 대부분 <0.10 ms)
- Token savings (12 turn 시뮬레이션):
  - budget=5000: 6050→3005 tokens (50 % 절감)
  - budget=2000: 6050→2000 tokens (67 % 절감)
  - budget=800:  6050→795 tokens (87 % 절감)
- Bit-determinism: 동일 input → 동일 output (test_deterministic_same_input_same_output 으로 검증)

**Relevance score 가중치 (frozen in advisor_hash):**
- agent_state_embedding cosine: 0.45
- task_progress_score 매치: 0.20
- composite_novelty 근접도: 0.10
- recency (half-life 8 turns): 0.25

**Threshold (frozen in advisor_hash):**
- score ≥ 0.70 → keep_verbatim
- 0.30 ≤ score < 0.70 → summarize (~30 % 압축 가정)
- score < 0.30 → drop

### 5.7 v3.8 Group-commit + persistent perf snapshot

```
src/aegis/audit/group_commit.py               ←  Claim 51
src/aegis/audit/encrypted_journal.py          ←  encrypt() / serialize() 분리 + os.fsync()
src/aegis/performance/feedback_snapshot.py    ←  Claim 53
tests/unit/test_journal_group_commit.py       ←  10 unit tests (PASS)
tests/unit/test_feedback_snapshot.py          ←  11 unit tests (PASS)
```

**Throughput 측정:** group_commit 활성화 시 동일 NVMe 에서 fsync per-call
대비 batch_size N 만큼 throughput 증가 (예: batch=100 → ~100×).
RPO 동일 (caller 가 fsync 완료 후 return).

**Snapshot 측정:**
- SQLite WAL mode + INSERT OR REPLACE per (tenant, aid) row
- 1000 keys 기준 ~80 KB write
- 30 sec interval default → RPO ≤30 sec (advisor advisory-only 라 충분)

### 5.8 v3.9 Tiered archive

```
src/aegis/audit/tiered_archive.py             ←  Claim 52
tests/unit/test_tiered_archive.py             ←  16 unit tests (PASS)
```

**3-tier durability (whitepaper §2 pattern B):**
- Hot tier: 활성 NVMe (`audit_encrypted.jsonl`)
- Warm tier: 회전된 segment (`audit_encrypted.0001.jsonl`, ...) — `hot_retention_segments` 만큼 보존
- Cold tier: `FilesystemArchive` (default) 또는 `S3ArchiveStub` (production: boto3)

**Cross-tier replay 보존:** archive 된 segment 도 동일 데이터 키로
`EncryptedJournal` 가 그대로 decrypt — `test_archived_segment_remains_decryptable`
로 검증.

**Hot tier 압력 제거:** N segments 만 SSD 에 보유, 90 % 이상 archive →
storage 비용 ~5–10× 절감 (cold tier 가 SSD 보다 30–50× 저렴).

### 5.9 v4.0 Audit patrol

```
src/aegis/audit/patrol.py                     ←  Claim 54
src/aegis/api/audit_patrol.py                 ←  GET /audit/patrol/status
                                                  POST /audit/patrol/run
tests/unit/test_audit_patrol.py               ←  26 unit tests (PASS)
```

**6 검증 × 5 cadence (default):**
- sequence gap (ATMU intent_log) — every 5 min
- random 1 % sample (audit DB) — every 1 hour
- cross-store consistency — every 1 hour
- full chain re-verify (audit DB + cost ledger) — every 6 hours
- cold-tier sample decrypt — every 24 hours

**측정값:**
- 1000 records full sweep ≈ 50 ms (M3 Mac)
- 5 min cadence × sequence patrol = ~10 KB SQLite read
- Findings 발생 시 즉시 `recent_reports()` 에 보존, 50 reports rolling

**Operator UX:**
- `GET /audit/patrol/status` — 가장 최근 status + 20 reports history
- `POST /audit/patrol/run` body=`{"scope":"sequence|sample|full|consistency|cold"}` — 즉시 1회 실행

### 5.10 v4.1 HW telemetry collectors

```
src/aegis/hw_telemetry/collectors/__init__.py     ←  package + Protocol
src/aegis/hw_telemetry/collectors/base.py         ←  HWCollector + CollectorResult
src/aegis/hw_telemetry/collectors/{pmu,edac,iommu,ethtool,nvml}.py  ← real
src/aegis/hw_telemetry/collectors/bmc_redfish.py                    ← out-of-band
src/aegis/hw_telemetry/collectors/{mock_tee_quote,mock_aegis_fpga}.py ← T2 mock
src/aegis/hw_telemetry/collectors/aggregator.py   ←  Claim 55
tests/unit/test_hw_collectors.py                  ←  30 unit tests (PASS)
```

**Source coverage on T2 environment:**
- ✅ CPU PMU (Linux `/proc/stat`) — `gpu_utilization` proxy
- ✅ DRAM ECC (Linux EDAC `/sys/devices/system/edac/`) — `ecc_correctable/uncorrectable`
- ✅ IOMMU (`/sys/kernel/iommu_groups/`) — `dma_fanout`
- ✅ NIC (`/proc/net/dev`) — `network_bytes_in/out`
- ✅ NVIDIA GPU (NVML, optional) — `gpu_utilization`, `hbm_*`, `thermal_*`
- ✅ BMC Redfish (HTTP, opt-in via `AEGIS_BMC_REDFISH_URL/_TOKEN`)
- ⏳ TEE quote (mock; real on T3 — TDX/SEV-SNP/CCA)
- ⏳ Aegis-FPGA (mock; real on M21+ silicon)

**Env-driven path:**
- `AEGIS_HW_PROVIDER=none` — zero-fill (T2 default)
- `AEGIS_HW_PROVIDER=sim` — v2.3 deterministic SHA3 simulator
- `AEGIS_HW_PROVIDER=real` — **v4.1 collector aggregator** (NEW)

---

## 6. 출원 전 체크리스트

- [x] Reference implementation: `src/aegis/performance/`, `src/aegis/judge/unified_head.py`, `src/aegis/audit/{group_commit,tiered_archive,patrol}.py`, `src/aegis/hw_telemetry/collectors/`
- [x] Unit tests: **1075 passed** (905 → 1075, +170, 1 skipped — llama-cpp 미설치)
- [x] Type-check clean: mypy 113 source files
- [x] Lint clean: ruff
- [x] HTTP endpoints exposed: `/advisory/{kv_cache,scheduling,placement,all,unified,context}`, `/audit/patrol/{status,run}`
- [x] Demos: `demo/kv_cache_advisor.py`, `demo/runtime_closed_loop.py`, `demo/context_advisor.py`
- [x] vLLM design doc: `docs/VLLM_INTEGRATION_DESIGN.md`
- [x] **Production durability primitives** (v3.8/v3.9): group-commit + perf snapshot + tiered archive
- [x] **Audit patrol** (v4.0, Claim 54): 6-check periodic integrity verification
- [x] **HW telemetry collectors** (v4.1, Claim 55): 8-source aggregator + graceful degradation
- [ ] vLLM 실제 환경 벤치마크 (cache_hit_rate uplift) — v4.x milestone
- [ ] 학습된 unified head 가중치 — v4.x milestone
- [ ] Subfield-selective ATV diff 압축 (Claim 49) — v3.x/v4.x
- [ ] Unified head v2 (5 outputs, Claim 50) — v3.x
- [ ] Replicated WAL / Raft (whitepaper §2 pattern D) — v4.x cluster mode
- [ ] T3 hardware 의 cost-attestation key 서명 통합 — M19+ 시점

---

## 7. 후속 milestone (v4.x)

1. **Learned unified head** — 손-튜닝 (heuristic) 가중치를 학습된
   M13 weights 로 교체. 트러스트 + 퍼포먼스 양면의 ground truth 신호로
   joint training. v3.6 의 architectural seam 위에 그대로 plug.
2. **vLLM 실 환경 통합** — 본 보강 §5 의 reference shim 을 실제
   vLLM 환경에서 컴파일 / 벤치마크.
3. **vLLM upstream PR** — `BlockManager` 의 plug-point 를 upstream 으로
   제안 (subclass 강제 없이 hook 으로).
4. **Federation (Claim 47)** — cross-tenant batch_key 공유.
5. **Hardware closed loop** — T3 silicon (M19+) 의 cost-attestation key 로
   런타임 측정치 서명 → signed closed-loop attestation.

---

## 8. 참고

- 본 보강은 **기존 출원의 dependent claim** 으로 추가 출원 권고.
- 주 출원: `ATV_v7_10` (40 claims, 본 출원의 모체)
- 본 보강의 모든 reference implementation 은
  [happyikas/Aegis-ATV](https://github.com/happyikas/Aegis-ATV) 의
  `main` 브랜치에 v3.6.0 tag 로 동결됨.
- 968 자동 테스트 PASS, mypy/ruff clean, IEEE-754 결정론적.
