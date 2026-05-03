# Solo Free 진짜 sLLM + 진짜 Embedding 활성화 가이드

> Claude Code 싱글 사용자용 무료 배포 — 외부 API 키 / 결제 없이 **실제
> 로컬 LLM** 으로 verdict 를 내고 **실제 BGE encoder** 로 ATV 의
> agent_state 를 의미 있는 768-D 벡터로 임베딩하는 전체 셋업.

소요 시간: **약 12분** (Llama 770MB + BGE 100MB 다운로드).
필요 디스크: **~1.1GB** (모델 두 개 + llama-cpp wheel).

---

## 1. 왜 이게 필요한가

`aegis install --mode local --judge hybrid` 만 돌리면 다음이 동작합니다:

| 컴포넌트 | 무엇? | 진짜 LLM? |
|---|---|:---:|
| Judge Tier 1 | M13 attribution head — 30-feature 선형 분류기 | ❌ |
| Judge Tier 2 | LocalPhi (stub) — Tier 1 wrapper | ❌ |
| Judge Tier 4 | Dummy regex — fallback | ❌ |
| Embedding | DummyEmbedding — SHA3 noise | ❌ |

→ **실제 LLM 토큰 호출 0회 + 의미 없는 임베딩.**

이 가이드를 따르면:

| 컴포넌트 | 무엇? | 진짜 LLM? |
|---|---|:---:|
| Judge Tier 1 | M13 attribution head | ❌ (빠른 1차, 0.3ms) |
| **Judge Tier 2** | **LocalPhi (real) — Llama-3.2-1B-Q4 on-device** | **✅ ~80ms** |
| Judge Tier 4 | Dummy regex | ❌ (fallback) |
| **Embedding** | **BGELocalEmbedding — BGE-base-en-v1.5 768-D** | **✅ ~10ms** |

Tier 1 이 결정 못 한 gray-zone 만 Tier 2 LLM 으로 escalate. 평균
시나리오의 80%는 Tier 1 (1ms) 에서 끝나고, 나머지 20% 만 LLM 호출.

**Embedding 의 가치:** ATV 의 `agent_state_embedding` (768-D) 슬롯이
의미 있는 벡터로 채워짐. 현재는 M13 weight 가 0 이라 verdict 에 직접
영향 없지만, 다음 트랙들의 발판:
1. **M13 v2 학습** — Burn-in Shadow phase 에서 (ATV, verdict) 쌍 수집
   할 때 noise 위가 아닌 진짜 의미 위에서 학습
2. **RAG step340** — "이 호출과 코사인 유사한 과거 BLOCK 사례 N개" 를
   LLM prompt 에 주입 (1B 모델의 정확도 약점 보완)
3. **session_behavioral_drift** — 세션 시작 시점 vs 현재 의미 거리

---

## 2. 셋업 (4 명령어)

```bash
# 1. 모델 두 개 다운로드 (Llama-3.2-1B Q4 770MB + BGE-base-en Q4 100MB)
uv run aegis pull-model                            # judge GGUF
uv run aegis pull-model --model bge-base-en        # embedding GGUF

# 2. llama-cpp-python 설치 (Apple Silicon Metal 가속)
CMAKE_ARGS="-DGGML_METAL=on" uv sync --extra local-llm

# 3. .env 에 모델 경로 등록
echo "AEGIS_JUDGE_MODEL_PATH=$(pwd)/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf" >> .env
echo "AEGIS_EMBEDDING_MODEL_PATH=$(pwd)/models/bge-base-en-v1.5-q4_k_m.gguf" >> .env
echo "AEGIS_EMBEDDING_PROVIDER=bge-local" >> .env
```

설치 검증:

```bash
./scripts/dogfood_check.sh
```

마지막에 다음이 보이면 ✅:

```
[9] Real local-sLLM verdict
  ✓ real Llama verdict: ALLOW  (2489 ms, model_hash matches file SHA3)
[10] Real BGE embedding
  ✓ real BGE: cos(destructive, destructive)=0.820 > cos(destructive, benign)=0.490  (341 ms)
✓ 9/9 checks passed — green-light for real Claude Code
```

`[9]` / `[10]` 이 `skipped` 면 §6 트러블슈팅 참조.

---

## 3. Claude Code 에 연결

```bash
uv run aegis install --mode local --judge hybrid --embedding bge-local --force
```

설치 시 다음 라인이 나와야 합니다:

```
[install] plugin v2.0.0, mode=local, judge=hybrid, embedding=bge-local
  ✓ local-sLLM ready: /…/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf (real LLM verdicts active)
  ✓ local embedding ready: bge-base-en-v1.5-q4_k_m.gguf (768-D real BGE encoder)
```

Claude Code 재시작 → 모든 tool 호출이 firewall + 진짜 sLLM 통과.

---

## 4. 다른 모델 옵션

```bash
uv run aegis pull-model --list
```

### Judge 모델 (verdict 내는 LLM)

| 모델 | 크기 | 속도 (M1 CPU) | 품질 | 추천 |
|---|---:|---:|:---:|---|
| `llama-3.2-1b` (default) | 770 MB | ~80 ms | ★★ | 균형 잡힌 기본 |
| `qwen-0.5b` | 400 MB | ~30 ms | ★ | 가장 작음, JSON 정확도 낮음 |
| `phi-3.5-mini` | 2.2 GB | ~150 ms | ★★★★ | 정확도 우선, 디스크 충분 |

### Embedding 모델 (ATV agent_state 인코딩)

| 모델 | 크기 | 차원 | 속도 (M1 CPU) | MTEB | 추천 |
|---|---:|---:|---:|---:|---|
| `bge-base-en` (default) | 100 MB | 768 | ~10 ms | 63.55 | ATV 768-D 슬롯과 정확히 매치 |
| `bge-small-en` | 33 MB | 384 | ~5 ms | 62.17 | 더 작음, 768-D 슬롯에 zero-pad |

```bash
uv run aegis pull-model --model phi-3.5-mini   # judge 더 강력
uv run aegis pull-model --model bge-small-en   # embedding 더 작게
```

`AEGIS_JUDGE_MODEL_PATH` / `AEGIS_EMBEDDING_MODEL_PATH` 만 새 GGUF 로
바꾸면 즉시 적용됨 (재설치 불필요).

---

## 5. 진짜 LLM 이 도는지 확인하는 법

### 5.1 `aegis report` (5-line 요약)

```bash
uv run aegis report -v
```

판단 라인에 `model_hash=f7afbc...` (파일 SHA3) 이 보이면 진짜 LLM.
`model_hash=stub-...` 이면 stub mode.

### 5.2 audit log 직접 확인

```bash
tail ~/.aegis/audit.jsonl | jq .
```

`reason` 에 `"local-phi (parsed): ..."` 또는 `"local-phi: ..."` 가
있으면 LLM 출력. `"local-phi (stub): ..."` 면 stub.

### 5.3 latency 확인

진짜 Judge LLM (Llama-1B):
- Cold call (첫 호출): 2-3초 (모델 메모리 로드)
- Warm call: 50–150ms (M1 CPU)

진짜 Embedding (BGE-base):
- Cold call: 1.5-2초 (메모리 로드)
- Warm call: 5-15ms (M1 CPU) — 매 verdict 마다 호출됨

Stub / dummy mode:
- 항상 <1ms

### 5.4 Embedding 이 진짜인지 확인

```bash
./scripts/dogfood_check.sh
```

`[10] Real BGE embedding` 라인:
- `cos(destructive, destructive)=0.820` ≈ 진짜 BGE 의 의미 군집
- `cos(destructive, benign)=0.490` ≈ 다른 의미는 거리 멀어짐

dummy 면 두 값 모두 ~0.0 (semantic 신호 없음). 0.10+ 차이 = 진짜.

---

## 6. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| dogfood [9] = skipped (no GGUF) | `uv run aegis pull-model` 안 돌림 |
| dogfood [9] = skipped (llama-cpp missing) | `uv sync --extra local-llm` 안 돌림 |
| `uv sync` 후 llama-cpp 사라짐 | `uv sync` 는 extras 를 strip — `uv sync --extra local-llm` 다시 |
| 진짜 LLM 인데 cold call 5초 초과 | Phi-3.5-mini 면 정상 (Mac mini 이전 모델). M1/M2 면 모델 검증 |
| Audit 가 항상 stub mode | `AEGIS_JUDGE_MODEL_PATH` 환경 변수 확인. `.env` 에 있어도 hook 시작 시 읽힘 |
| `llama_cpp_python` build 실패 | Metal: `xcode-select --install`. CUDA: `CMAKE_ARGS="-DGGML_CUDA=on"` |
| Hook latency 매번 80-150ms | 정상 — warm call 이지만 매 호출 LLM. 빠르게 하려면 Tier 1 만 (`--judge dummy`) |

---

## 7. 정확도 데이터 (Llama-3.2-1B Q4 + M13)

7개 시나리오 회귀 테스트:

```bash
./scripts/macmini_user_test.sh --hybrid
```

| 시나리오 | 잡힘 |
|---|:---:|
| Replit DB 삭제 | ✅ M13 (Tier 1) |
| AWS 시크릿 push | ✅ M13 (Tier 1) |
| CLAUDE.md drift | ✅ step309 |
| 명령 5번 루프 | ✅ M13 (Tier 1) |
| 1.4M 토큰 cost runaway | ✅ step335 |
| 멀티 에이전트 escalation | ✅ step308 |
| /etc/passwd 탐색 | ✅ step310 |

**7/7 PASS.** Tier 2 LLM 은 위 시나리오 중 어느 것도 결정에 *필수*
가 아닙니다 — Tier 1 이 다 잡음. LLM 의 가치는 **새로운 (rule 에
없는) 공격 패턴** 에 대한 일반화 능력.

### Tier 2 LLM 이 진짜 도는 case

`aegis report -v --since 1d | grep "local-phi: "` — gray-zone 호출 통계.
정상 사용 패턴에서는 5-15% 수준이 LLM tier 까지 escalate.

---

## 8. 데이터 / 프라이버시

| 항목 | Solo Free 보장 |
|---|---|
| 외부 API 호출 | ❌ 없음 (Tier 3 Haiku 는 API 키 없으면 stack 에 미포함) |
| 모델 다운로드 시 텔레메트리 | ❌ 없음 (HuggingFace 만, anonymous GET) |
| 모든 verdict 로컬 저장 | ✅ `~/.aegis/audit.jsonl` (SHA3 chained) |
| 모델 무결성 | ✅ `model_hash = SHA3-256(GGUF file)`. audit 마다 기록 |
| 결정 재현 | ✅ greedy decode (T=0, top_k=1) — 같은 입력 → 같은 출력 |

---

## 9. 디스크 / 성능 footprint

| 자원 | Solo Free 기본 |
|---|---|
| Judge GGUF | 770 MB (`models/Llama-3.2-1B-Instruct-Q4_K_M.gguf`) |
| Embedding GGUF | 100 MB (`models/bge-base-en-v1.5-q4_k_m.gguf`) |
| llama-cpp-python wheel | ~70 MB |
| Hook overhead per call | 5-150ms (대부분 1-5ms M13 + 5-10ms BGE) |
| RAM (Judge LLM 메모리) | ~1.2 GB (Llama-1B Q4 + KV cache) |
| RAM (Embedding 메모리) | ~150 MB (BGE-base-en) |
| RAM (firewall idle) | ~50 MB |
| **합계 (active 시)** | **~1.5 GB RAM, ~1 GB disk** |

8GB Mac mini 도 무리 없음. 4GB 머신에서는 `qwen-0.5b` + `bge-small-en`
권장 (~700 MB RAM).

---

## 10. 다음 단계

- 하루 사용 후 `aegis report -v --since 1d` → tier 사용 분포 확인
- `--model phi-3.5-mini` 로 업그레이드 (정확도 vs 디스크)
- 알려진 P1 follow-up: Llama-1B 가 1-of-N false-negative 패턴 (AWS
  secret in `Edit` args 등) — M13 의 `tool_arg_inspection` weight 보정 예정
