# AegisData 성능 개선 기술 백서

**같은 ATV 텐서로 LLM 추론을 가속한다 — 5 축 advisory surface**

**버전:** v3.7 (2026-04-28)
**대상 독자:** AI 인프라 결정권자, 플랫폼 엔지니어, ML 시스템 리뷰어, 투자자
**소요 시간:** 약 20 분

---

## 0. 한 줄 요약

AegisData 는 원래 AI 에이전트의 도구 호출을 **보안 검증**하는 사이드카입니다.
이 백서는 같은 시스템이 어떻게 LLM 추론 런타임의 **성능까지 동시에**
끌어올리는지 설명합니다.

핵심 통찰 한 줄:

> **보안 검증에 쓰이는 2080-D ATV 텐서를, KV cache 관리·스케줄링·메모리
> 배치·컨텍스트 윈도우 결정에 그대로 다시 사용한다.**

LLM 모델 코드는 한 줄도 수정하지 않습니다. 런타임 (vLLM, MLX-LM,
llama.cpp, SGLang) 옆에 HTTP 한 번 (≤5 ms) 으로 연결됩니다.

---

## 1. 배경 — 왜 필요한가

### 1.1 AI 에이전트 시대의 추론 부하

생성형 AI 가 단순 챗봇에서 **자율 에이전트** 로 진화하면서 LLM 추론
부하가 폭증했습니다:

- 한 명의 사용자가 한 번 묻고 답 받는 게 아니라, **에이전트 하나가 수
  십 turn 의 도구 호출**을 자동으로 수행.
- 같은 모델이 **여러 에이전트 / 여러 tenant** 의 요청을 병렬 처리.
- 각 turn 마다 컨텍스트가 누적되어 **token 사용량이 선형 증가**.

LLM 추론 비용은 다음 세 자원에 종속됩니다:

| 자원 | 부족하면 | 현재 관리 방식 |
|---|---|---|
| **HBM (GPU 메모리)** | KV cache 가 evict 되어 redo 비용 폭증 | LRU |
| **Context window** | token 한도 초과 → 모델이 자동 truncate | 자동 (task-aware 아님) |
| **GPU 시간** | latency 증가, batching 효율 저하 | FIFO + 우선순위 |

### 1.2 기존 LLM 서빙의 한계

세 자원 모두 **에이전트의 의도와 무관한** 휴리스틱으로 관리됩니다.

예를 들어:
- vLLM 의 PagedAttention 은 **LRU eviction** — 최근 안 본 KV block 을
  버립니다. 그러나 에이전트가 sub-task 를 잠시 미뤘다가 다시 돌아올 것이
  뻔한데도 LRU 는 그것을 알지 못합니다.
- LLM 자체의 자동 컨텍스트 압축 (Anthropic `/compact` 등) 은 token 수
  기준으로 잘릴 뿐, **현재 task 와 관련 없는 과거 phase** 를 우선
  버리지 않습니다.
- 스케줄러는 어떤 요청이 **interactive (사용자 대기 중)** 이고 어떤
  것이 **batch (백그라운드 자동 작업)** 인지 모릅니다.

이 모든 결정에는 **에이전트의 현재 상태와 의도** 가 결정적으로 중요한데,
정작 그 정보는 LLM 서빙 계층에 도달하지 못합니다.

### 1.3 AegisData 의 위치

AegisData 는 모든 도구 호출 직전에 끼어드는 **사이드카**입니다.
원래 목적은 보안 검증이지만, 그 과정에서 이미 **에이전트의 모든
상태 신호를 2080 차원 벡터로 수집**하고 있습니다.

```
   ┌──────────────┐    도구 호출       ┌──────────────┐
   │  에이전트     │ ─────────────────►│  Aegis 사이드카│
   └──────────────┘                    │              │
                                       │  ATV-2080 생성 │
                                       │  + 보안 검증   │
                                       └──────┬───────┘
                                              │
                                              │ 본 백서의 추가:
                                              │ 같은 ATV 로
                                              │ perf advisor 도 호출
                                              ▼
                                       ┌──────────────┐
                                       │  LLM 런타임    │
                                       │  (vLLM/MLX 등) │
                                       └──────────────┘
```

같은 ATV 를 **두 번 쓴다** 는 게 본 백서의 핵심입니다.

---

## 2. ATV 빠른 복습

### 2.1 ATV 는 무엇인가

ATV (Agent Telemetry Vector) 는 한 turn 의 에이전트 상태를 담는
**2080 차원 float32 텐서** 입니다. 미국 임시 특허 ATV_v7_10 의 핵심.

크기:
- 8 KB (= 2080 × 4 bytes) per turn
- LLM embedding 보다 작지만, **30 개 의미적 subfield** 로 구조화
- 결정론적 SHA3 해싱 → audit replay 가능

### 2.2 30 개 subfield

| subfield | 차원 | 의미 |
|---|---|---|
| `agent_state_embedding` | 768 | 에이전트의 현재 작업 상태 (텍스트 임베딩) |
| `action_history` | 640 | 최근 도구 호출 시퀀스 |
| `inter_agent_graph` | 128 | 다른 에이전트와의 연결 관계 |
| `memory_provenance` | 64 | 참조한 메모리의 해시 |
| `cost_efficiency_metrics` | 16 | token, dollar, cache_hit_rate, progress 등 |
| `tool_arg_inspection` | 32 | 인자에 위험 패턴이 있나 (rm, drop 등) |
| `action_blast_radius` | 16 | 호출의 파괴력 (read 1, send_email 8) |
| `prompt_structure` | 16 | 길이, 코드블록, 패턴 |
| `novelty_score` | 4 | 이 turn 이 얼마나 새로운가 |
| `human_oversight_state` | 8 | 운영자 in-the-loop 인가 |
| ...(나머지) | | |
| **HW band** | 200 | T3 hardware 시 채워짐 (현재 zero-fill) |

각 subfield 는 4 가지 family 중 하나로 인코딩됩니다:

- **TEXT-EMBED** — 의미 임베딩 (cosine 거리가 의미적 유사성)
- **HASH-EXPAND** — SHA3 결정성 expansion (구조 기반)
- **FEATURE-EXTRACT** — named-slot scalar features
- **ZERO** — HW band 미구현 영역

### 2.3 왜 LLM embedding 이 아닌가

LLM embedding 은 "의미를 벡터로 표현" 합니다. ATV 는 "에이전트의
**행동 상태**를 벡터로 표현" 합니다. 비유하자면 LLM embedding 이
**소설의 단어** 라면, ATV 는 **소설의 등장인물 카드** 입니다 — 누가,
무엇을, 어디서, 얼마나, 누구와, 비용은, 위험도는.

이 차이가 본 백서의 모든 perf advisor 가 가능한 이유입니다.

---

## 3. 핵심 통찰 — 같은 텐서를 다시 쓴다

기존 LLM 시스템은:

| 영역 | 사용 입력 |
|---|---|
| 안전성 검증 | 별도 firewall, Constitutional AI |
| KV cache 관리 | 자체 LRU, 메모리 압력 |
| Scheduling | 우선순위 큐 |
| Context 압축 | token count 기반 자동 |

→ **모두 분리된 시스템이고, 각자 다른 시그널을 사용**.

AegisData v3.6+ 는:

| 영역 | 사용 입력 |
|---|---|
| 안전성 검증 | **ATV-2080 + M13 head** |
| KV cache 관리 | **같은 ATV + KV cache advisor head** |
| Scheduling | **같은 ATV + scheduling advisor head** |
| Memory placement | **같은 ATV + placement advisor head** |
| Context 압축 | **같은 ATV + context advisor head** |

→ **하나의 텐서, 5 개 head, 5 가지 결정**.

이는 multi-task learning 에서의 **shared encoder** 와 같은 패턴입니다.
입력 표현은 비싸게 한 번 만들고, 그 위에 가벼운 출력 head 를 여러 개
얹어 다양한 결정을 동시 수행.

본 백서는 이 5 head 가 어떻게 동작하는지 설명합니다.

---

## 4. 5축 성능 개선 기법

### 4.1 KV Cache 자문 (v3.1)

**문제:** vLLM 같은 런타임은 LRU 로 KV cache 를 evict 합니다. 그런데
에이전트가 다음 turn 에 **다시 참조할 메모리** 가 어떤 것인지는 LRU 가
모릅니다.

**해법:** ATV 의 다음 신호로 **prefetch / evict 결정** 을 합니다.

| ATV 신호 | 의미 |
|---|---|
| `task_progress_score` | 작업이 얼마나 진행됐나 — 진행 많을수록 hot path |
| `composite_novelty` | 새로움 정도 — 낮으면 stable 해서 재방문 가능 |
| `cache_hit_rate` (s-10) | 직전 측정한 캐시 hit 률 |
| `context_utilization_ratio` (s-11) | 컨텍스트 압력 |
| `memory_provenance` | 참조한 메모리의 ID 해시 |
| `agent_state_embedding` | 같은 task = 같은 KV layout 후보 |

**출력 (KVCacheAdvice):**

```python
KVCacheAdvice(
    prefetch_segment_ids=["mem-abc12345", "iag-def67890"],  # 미리 GPU 로 가져와라
    evict_candidates=["hist-old1", "hist-old2"],            # 이건 내려도 안전
    residency_class="hot",   # hot | warm | cold
    batch_key="cohort-A",    # 같은 cohort 끼리 batching
    speculative_decode=True, # speculative 가능한 turn 인가
    confidence=0.85,
    advisor_hash="...",
)
```

**런타임 매핑 (예: vLLM):**

```
prefetch_segment_ids → BlockManager.pin() + 비동기 H2D 복사
evict_candidates     → eviction priority queue
batch_key            → Scheduler 의 cohort group
speculative_decode   → draft model on/off
```

**결과:**
- 평균 latency: **0.011 ms** (p50), 0.035 ms (p99) — M3 Mac 측정
- 100% bit-deterministic (같은 ATV → 같은 결정)
- Pure function (외부 I/O 없음)

### 4.2 Closed-loop 피드백 (v3.2)

**문제:** ATV 의 `cache_hit_rate` (s-10) 슬롯은 호스트가 채워야 합니다.
그런데 호스트가 첫 호출 시점에서 이 값을 알 수 없습니다 (아직 측정
안 됐으니까).

**해법:** **런타임이 측정한 perf 를 다음 turn 의 ATV 에 backfill**.

```
Turn 1: 호스트 → Aegis (cache_hit_rate=0.0, advisor confidence 낮음)
        Aegis → 런타임 → 디코딩 → 측정: cache_hit_rate = 0.85
        런타임 → POST /tool-outcome → Aegis EWMA 저장

Turn 2: 호스트 → Aegis (cache_hit_rate=0.0)
        Aegis 가 EWMA 로 backfill (cache_hit_rate=0.85)
        advisor confidence 높음 → 정확한 결정
```

EWMA (지수 가중 이동평균):
- α = 0.30 (최근 30 %, 과거 70 %)
- per (tenant_id, aid) 키
- 호스트가 명시 값을 줬으면 절대 덮어쓰지 않음 (호스트 우선권)

**결과:** 8-turn 시뮬레이션에서 advice confidence 가
**0.40 → 0.85 으로 monotonically 상승**.

### 4.3 Scheduling 자문 (v3.4)

**문제:** LLM 서빙 scheduler 는 어떤 요청이 interactive (사용자가 화면
앞에서 대기) 이고 어떤 것이 batch (자동 실행) 인지 모릅니다. 무거운
batch 가 interactive 앞을 막으면 사용자 경험이 망가집니다.

**해법:** ATV 의 다음 신호로 **priority class** 를 결정합니다.

| ATV 신호 | 의미 |
|---|---|
| `human_oversight_state.operator_present` | 운영자가 보고 있나 → interactive |
| `action_blast_radius.blast_radius_norm` | 파괴력이 큰가 → 즉시 처리 |
| `composite_novelty` | 새로움 → 우선순위 |
| `tool_arg_inspection.destructive_verb` | 위험 동사 → preempt 안전성에 영향 |

**출력 (SchedulingAdvice):**

```python
SchedulingAdvice(
    priority_class="interactive",  # interactive | batch | low
    preempt_safe=False,            # 이걸 일시 정지해도 안전한가
    max_concurrent_in_cohort=8,    # 같은 cohort 동시 실행 가능 수
    deadline_ms=2000,              # 권장 SLA
    confidence=0.75,
)
```

**예시 결정:**
- 운영자가 채팅 중 → interactive, deadline 2 초
- 새벽 배치 잡 → batch, deadline 30 초
- 백그라운드 인덱싱 → low, deadline 60 초

### 4.4 Memory Placement 자문 (v3.4)

**문제:** 모델 가중치를 어디에 둘 것인가 (HBM / CPU / SSD/CSD)?
KV cache 는 어떤 정밀도로 양자화할 것인가 (f16 / q8 / q4)?
지금까지는 deployment 시 정적으로 결정되거나, 메모리 압력 발생 시
런타임이 즉흥 결정.

**해법:** ATV 의 압력 신호로 **per-layer tier 결정**.

| ATV 신호 | 의미 |
|---|---|
| `cache_hit_rate` (s-10) | 낮으면 압력 큼 |
| `context_utilization_ratio` (s-11) | 클수록 압력 큼 |
| `composite_novelty` | 낮으면 stable 한 prefetch 안전 |
| `aid_ats_scalars[4]` (T3 flag) | T3 면 CSD tier 사용 가능 |

**출력 (PlacementAdvice):**

```python
PlacementAdvice(
    layer_residency_plan={0: "hbm", 1: "hbm", ..., 16: "cpu", ..., 31: "hbm"},
    kv_quantisation_dtype="q8_0",        # f16 | q8_0 | q4_0
    prefetch_window_tokens=128,
    swap_threshold_bytes=2_000_000_000,  # 2 GB 남으면 swap 시작
    confidence=0.70,
)
```

**전략:**
- 임베딩 + LM head (소형, 매 턴 사용) → 항상 HBM
- 중간 transformer block → 압력 따라 demote
- **고압 + T3 hardware** → cold middle layers 를 CSD 로 (M19+ 하드웨어 마일스톤)

### 4.5 Context Window 자문 (v3.7)

**문제:** 에이전트가 50 turn 동안 작업하면 context 가 ~250 K tokens
누적. 자동 truncation 은 단순 LRU 라 **현재 task 와 무관한 과거
phase 를 우선 버리지 않습니다**.

**해법:** ATV 의 turn-to-turn 거리로 **per-turn relevance** 계산 후
token budget 안에서 **그리디 ROI 정렬** 로 keep / summarize / drop 결정.

**Relevance score 공식 (frozen):**

```
score = 0.45 × cosine(현재 ATV.agent_state, 과거 ATV.agent_state)
      + 0.20 × |현재 progress - 과거 progress| 매치도
      + 0.10 × |현재 novelty - 과거 novelty| 근접도
      + 0.25 × exp(-turns_back / 8)              ← recency
```

**Threshold (frozen):**
- score ≥ 0.70 → **keep_verbatim** (그대로 유지)
- 0.30 ≤ score < 0.70 → **summarize** (~30 % 압축)
- score < 0.30 → **drop** (제거)

**예시 — 12-turn 대화:**

```
turn 0-3:  "explore-codebase" phase  (오래됨, 다른 phase)
turn 4-7:  "refactor-auth" phase     (중간, 다른 phase)
turn 8-11: "fix-import-bug" phase    (최근, 같은 phase)
현재:      "fix-import-bug" 진행 중

token budget = 2000 일 때 결정:
turn 11 (score 0.99) → keep
turn 10 (score 0.95) → keep
turn 09 (score 0.90) → keep
turn 08 (score 0.86) → keep
turn 07 (score 0.63) → summarize
turn 06 (score 0.59) → drop
... (모든 과거 phase) → drop

원래: 6050 tokens → 절감 후: 2000 tokens (67% 절감)
```

**결과:**
- 50-turn history 처리: **0.087 ms** (M3 Mac)
- 12-turn 시뮬레이션:
  - budget=5000 → 50 % 절감
  - budget=2000 → 67 % 절감
  - budget=800 → 87 % 절감

---

## 5. 통합 — Unified Head (v3.6)

### 5.1 단일 ATV pass, 4 출력

위 4 개 advisor (KV cache + scheduling + placement + 향후 context)
는 모두 같은 ATV 를 입력으로 받습니다. **한 번 ATV 를 만들고, 한 번
forward pass** 하면 4 종 출력을 모두 얻을 수 있습니다.

```python
class UnifiedHead:
    def evaluate_unified(self, atv, inp) -> UnifiedVerdict:
        verdict = self.attribution_head.evaluate_full(...)   # 트러스트
        kv      = kv_cache_advisor(atv, inp)                  # KV cache
        sch     = scheduling_advisor(atv, inp)                # 스케줄링
        pl      = placement_advisor(atv, inp)                 # 메모리 배치

        # 4 출력의 advisor_hash 들을 정렬-결합한 SHA3-256
        unified_hash = sha3_256(sort([
            verdict.model_hash, kv.advisor_hash,
            sch.advisor_hash, pl.advisor_hash,
        ])).hexdigest()

        return UnifiedVerdict(verdict, kv, sch, pl, unified_hash, ...)
```

### 5.2 unified_hash 의 audit 가치

`unified_hash` 가 4 head 의 버전을 모두 묶기 때문에:

- **하나라도 가중치 변경** → unified_hash 변경 → 감사 시 즉시 검출
- `aegis verify-audit` 가 트러스트 + 퍼포먼스 결정 전체를 한 번에 재현 가능
- 규제 측에서 "누가 어떤 perf 결정을 내렸나" 를 결정론적으로 추적

---

## 6. Advisory-only 프로토콜의 의의

### 6.1 권고 (advisory) 만 한다, 강제하지 않는다

본 시스템의 모든 자문은 **권고** 입니다. 런타임은:

- 자문을 **적용** 할 수도
- **부분 적용** 할 수도
- **완전히 무시** 할 수도

있습니다. Aegis 가 5 ms 안에 응답하지 못하거나, `confidence < threshold`
로 신뢰가 낮으면 런타임은 자체 LRU / FIFO 휴리스틱으로 **graceful
fallback** 합니다.

### 6.2 LLM 모델 코드는 한 줄도 안 건드린다

이 advisory-only 패턴 덕분에:

| 컴포넌트 | 변경 필요? |
|---|---|
| LLM 모델 가중치 | ❌ |
| Inference 코드 (forward pass) | ❌ |
| 토크나이저 | ❌ |
| Attention 구현 | ❌ |
| BlockManager / Scheduler | ✅ 작은 plug-in 추가 (예: vLLM ~150 LOC) |

### 6.3 vLLM / MLX-LM / llama.cpp 어느 것도 fork 안 한다

각 런타임을 위한 **HTTP 어댑터** (50–200 LOC) 만 작성하면 됩니다:

- `integrations/mlx_lm/` — `MLXLMAegisAdvisor` (Apple Silicon)
- `integrations/llama_cpp/` — `LlamaCppAegisAdvisor` (CPU/GPU 경량)
- `integrations/vllm/` — `VLLMAegisAdvisor` (NVIDIA 데이터센터)

설계 문서: [`docs/VLLM_INTEGRATION_DESIGN.md`](VLLM_INTEGRATION_DESIGN.md)

---

## 7. 실측 성능

### 7.1 Latency (M3 Mac, 2026-04)

| Advisor | Median latency | p99 |
|---|---:|---:|
| KV cache | 0.011 ms | 0.035 ms |
| Scheduling | <0.01 ms | 0.02 ms |
| Placement | 0.01 ms | 0.03 ms |
| Context (50-turn) | 0.087 ms | 0.15 ms |
| Unified head (4 output) | <1 ms | <2 ms |

→ **4 advisor 모두 합쳐도 sub-millisecond**.

### 7.2 자동 테스트 통과율

- 982 tests PASS (1 skip — llama-cpp 미설치)
- mypy 97 source files clean
- ruff clean
- 12-incident donor KPI + 7-시나리오 회귀 0
- 6/6 simulator HW attack 모두 차단

### 7.3 결정론

- **같은 ATV → 같은 advisor 출력** (bit-identical)
- IEEE-754 deterministic (CPU/GPU 무관)
- 자문 함수의 코드 버전이 `advisor_hash` 에 박혀 있어 audit replay 시 검출

### 7.4 실 효과 (시뮬레이션)

**Closed-loop 데모 (8-turn):**

| Turn | residency | confidence | measured cache_hit_rate |
|---|---|---:|---:|
| 1 | warm | 0.40 | 0.56 |
| 2 | warm | 0.61 | 0.50 |
| 3 | hot | 0.76 | 0.88 |
| 4 | hot | 0.85 | 0.87 |
| 5+ | hot | 0.85 | 0.92+ |

→ **EWMA 가 3 turn 내에 수렴**, advisor confidence 두 배 상승.

**Context advisor 데모 (12-turn):**

| Token budget | Tokens after | 절감률 |
|---:|---:|---:|
| 5000 | 3005 | 50 % |
| 2000 | 2000 | 67 % |
| 800 | 795 | 87 % |

---

## 8. 한계와 향후 방향

### 8.1 v3.x 의 한계

- **하드웨어 cost-attestation 미구현** — T3 silicon (M19+) 마일스톤 대기
- **vLLM 실 환경 벤치마크 미수행** — reference shim 만 동결, 실제
  vLLM 통합 / 측정은 v4.x
- **Advisor 가중치 hand-tuned** — 학습된 통합 head 는 v4.x

### 8.2 v4.x 로드맵

| 마일스톤 | 내용 | 예상 효과 |
|---|---|---|
| Learned unified head | 손-튜닝 가중치 → 학습 가중치 | task-aware 정확도 +20% |
| vLLM upstream PR | BlockManager hook 표준화 | 사용자 fork 불필요 |
| Cross-tenant federation | 같은 task phase tenant 간 KV 공유 | 멀티-테넌트 비용 -30 % |
| HW closed loop (T3) | cost-attestation 키로 measured perf 서명 | audit-grade telemetry |
| Subfield-selective ATV diff | 컨텍스트 압축 더 강화 | 추가 -20 % token |
| Unified head v2 (5 output) | context advisor 도 통합 | 단일 호출로 5 결정 |

### 8.3 비-목표

본 시스템은 다음 영역을 **명시적으로 다루지 않습니다**:

- LLM 자체의 정확도 향상 (별도 fine-tuning 영역)
- 토크나이저 최적화
- 모델 양자화 알고리즘
- GPU kernel 최적화

이 영역들은 **모델 코드 변경 영역** 이며, advisory-only 프로토콜의
설계 철학과 직교합니다.

---

## 9. 결론

### 9.1 무엇이 새로운가

기존 LLM 시스템은 **트러스트와 퍼포먼스를 분리된 시스템** 으로
다뤘습니다. AegisData 는 **하나의 ATV 텐서로 둘 다** 결정합니다.

이는 multi-task learning 의 **shared encoder** 패턴이지만, 적용
영역이 (a) AI 에이전트의 도구 호출 보안, (b) LLM 추론 성능 이라는
점이 새롭습니다.

### 9.2 왜 advisory-only 인가

LLM 서빙 생태계는 vLLM, SGLang, MLX-LM, llama.cpp 등으로 파편화되어
있고 각자의 변경 주기가 빠릅니다. 어떤 단일 런타임을 fork 하지 않고
**HTTP 한 번** 으로 결합되는 advisory-only 패턴은:

- 다양한 런타임에 동시에 적용 가능
- 런타임 release 와 독립적으로 발전
- 자문이 잘못되거나 끊겨도 런타임은 native 동작

### 9.3 특허적 위치

본 시스템은 미국 임시 특허 `ATV_v7_10` (40 청구항) 의 dependent
claim 으로 **Claims 41–48** 을 제안합니다 (`docs/PATENT_SUPPLEMENT_v3.md`):

- Claim 41: KV cache 자문 헤드
- Claim 42: Closed-loop perf attestation
- Claim 43: Scheduling 자문
- Claim 44: Memory placement 자문
- Claim 45: Unified attribution head
- Claim 46: Advisor-as-hint 프로토콜
- Claim 47: Cross-tenant federation (예약)
- Claim 48: Context window 자문

### 9.4 한 줄 결어

> **같은 ATV 텐서가 보안과 성능을 모두 끌어올린다.** 이는 AegisData 가
> 단순 firewall 이 아닌 **AI 에이전트 시대의 인프라 envoy** 라는 사실을
> 가장 잘 보여주는 사례입니다.

---

## 부록 A — HTTP API 요약

| Endpoint | 입력 | 출력 |
|---|---|---|
| `POST /evaluate` | ATVInput | Verdict (트러스트, 기존) |
| `POST /tool-outcome` | record_id + perf metrics | EWMA 갱신 |
| `POST /advisory/kv_cache` | ATVInput | KVCacheAdvice |
| `POST /advisory/scheduling` | ATVInput | SchedulingAdvice |
| `POST /advisory/placement` | ATVInput | PlacementAdvice |
| `POST /advisory/all` | ATVInput | 위 3 종 한 번에 |
| `POST /advisory/unified` | ATVInput | Trust + 위 3 종 (unified_hash) |
| `POST /advisory/context` | current ATV + history + budget | ContextAdvice |

모든 advisory 엔드포인트:
- p99 ≤ 5 ms
- Pure function (외부 I/O 없음)
- Bit-identical output for identical input
- `advisor_hash` 가 코드 버전 식별

## 부록 B — 용어 정리

| 용어 | 의미 |
|---|---|
| **ATV** | Agent Telemetry Vector — 2080 차원 에이전트 상태 벡터 |
| **Subfield** | ATV 의 30 개 의미 영역 |
| **M13 head** | 30 subfield 위의 frozen linear classifier |
| **Advisor** | ATV 를 읽고 perf 결정을 내는 pure function |
| **Unified head** | 트러스트 + 3 perf advisor 를 single forward pass 로 묶은 것 |
| **EWMA** | Exponential Weighted Moving Average — 측정값 누적 |
| **Cohort** | batch_key 가 같은 요청 그룹 |
| **Residency class** | KV cache 의 메모리 tier (hot/warm/cold) |
| **T3** | 하드웨어 tier (TEE / FPGA / CSD silicon) |
| **CSD** | Computational Storage Drive — NVMe + 연산 |

## 부록 C — 참조 구현 위치

| 모듈 | 경로 |
|---|---|
| KV cache advisor | `src/aegis/performance/kv_cache_advisor.py` |
| Scheduling advisor | `src/aegis/performance/scheduling_advisor.py` |
| Placement advisor | `src/aegis/performance/placement_advisor.py` |
| Context advisor | `src/aegis/performance/context_advisor.py` |
| Closed-loop feedback | `src/aegis/performance/feedback.py` |
| Unified head | `src/aegis/judge/unified_head.py` |
| HTTP endpoints | `src/aegis/api/advisory.py` |
| MLX-LM 어댑터 | `integrations/mlx_lm/__init__.py` |
| llama.cpp 어댑터 | `integrations/llama_cpp/__init__.py` |
| vLLM 어댑터 | `integrations/vllm/__init__.py` |
| 데모 | `demo/kv_cache_advisor.py`, `demo/runtime_closed_loop.py`, `demo/context_advisor.py` |

## 부록 D — 데모 실행 방법

```bash
# 환경 설정
uv sync
export AEGIS_EMBEDDING_PROVIDER=dummy
export AEGIS_JUDGE_PROVIDER=dummy

# 1) KV cache 자문 — 5 시나리오
uv run python demo/kv_cache_advisor.py

# 2) Closed-loop 시뮬레이션 — 8 turn 동안 confidence 상승
uv run python demo/runtime_closed_loop.py

# 3) Context window 자문 — 12-turn, 3 가지 budget
uv run python demo/context_advisor.py
```

각 데모는 외부 서비스 / API 키 없이 동작합니다.

## 부록 E — 신뢰성 / 결정성 검증

```bash
# 전체 자동 테스트 (982 통과)
uv run pytest -q

# 타입 검사
uv run mypy src

# 린트
uv run ruff check .
```

성공 시 출력:
```
982 passed, 1 skipped
Success: no issues found in 97 source files
All checks passed!
```

---

**문서 끝.**
질문 / 피드백 : `docs/PATENT_SUPPLEMENT_v3.md` 또는 GitHub Issues 참조.
