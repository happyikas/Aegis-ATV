# Solo Free 진짜 sLLM 활성화 가이드

> Claude Code 싱글 사용자용 무료 배포 — 외부 API 키 / 결제 없이 **실제
> 로컬 LLM** 으로 tool 호출 verdict 를 받는 전체 셋업.

소요 시간: **약 10분** (770MB GGUF 다운로드 시간 포함).
필요 디스크: **~1GB** (모델 + llama-cpp wheel).

---

## 1. 왜 이게 필요한가

`aegis install --mode local --judge hybrid` 을 그냥 돌리면 다음이 동작합니다:

| Tier | 무엇? | LLM? |
|---|---|:---:|
| 1 | M13 attribution head — 30-feature 선형 분류기 | ❌ |
| 2 | LocalPhi (stub mode) — Tier 1 결과 wrapper | ❌ |
| 4 | Dummy regex — fallback | ❌ |

→ **실제 LLM 토큰 호출 0회.** 정규식 + 선형 분류기로만 보호.

이 가이드를 따르면 Tier 2 가 진짜 Llama-3.2-1B 로 바뀝니다:

| Tier | 무엇? | LLM? |
|---|---|:---:|
| 1 | M13 attribution head | ❌ (빠른 1차) |
| **2** | **LocalPhi (real) — Llama-3.2-1B-Q4 on-device** | **✅** |
| 4 | Dummy regex | ❌ (fallback) |

Tier 1 이 결정 못 한 gray-zone 만 Tier 2 LLM 으로 escalate. 평균
시나리오의 80%는 Tier 1 (1ms) 에서 끝나고, 나머지 20% 만 LLM (~80ms)
호출.

---

## 2. 셋업 (3 명령어)

```bash
# 1. 모델 다운로드 (Llama-3.2-1B Q4, 770 MB)
uv run aegis pull-model

# 2. llama-cpp-python 설치 (Apple Silicon Metal 가속)
CMAKE_ARGS="-DGGML_METAL=on" uv sync --extra local-llm

# 3. .env 에 모델 경로 등록
echo "AEGIS_JUDGE_MODEL_PATH=$(pwd)/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf" >> .env
```

설치 검증:

```bash
./scripts/dogfood_check.sh
```

마지막에 다음이 보이면 ✅:

```
[9] Real local-sLLM verdict
  ✓ real Llama verdict: ALLOW  (2489 ms, model_hash matches file SHA3)
✓ 8/8 checks passed — green-light for real Claude Code
```

`[9]` 가 `skipped` 면 §6 트러블슈팅 참조.

---

## 3. Claude Code 에 연결

```bash
uv run aegis install --mode local --judge hybrid --force
```

설치 시 다음 라인이 나와야 합니다:

```
[install] plugin v2.0.0, mode=local, judge=hybrid
  ✓ local-sLLM ready: /…/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf (real LLM verdicts active)
```

Claude Code 재시작 → 모든 tool 호출이 firewall + 진짜 sLLM 통과.

---

## 4. 다른 모델 옵션

```bash
uv run aegis pull-model --list
```

| 모델 | 크기 | 속도 (M1 CPU) | 품질 | 추천 |
|---|---:|---:|:---:|---|
| `llama-3.2-1b` (default) | 770 MB | ~80 ms | ★★ | 균형 잡힌 기본 |
| `qwen-0.5b` | 400 MB | ~30 ms | ★ | 가장 작음, JSON 정확도 낮음 |
| `phi-3.5-mini` | 2.2 GB | ~150 ms | ★★★★ | 정확도 우선, 디스크 충분 |

```bash
uv run aegis pull-model --model phi-3.5-mini   # 더 강력한 모델로 바꾸기
```

`AEGIS_JUDGE_MODEL_PATH` 만 새 GGUF 로 바꾸면 즉시 적용됨 (재설치 불필요).

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

진짜 LLM:
- Cold call (첫 호출): 2-3초 (모델 메모리 로드)
- Warm call: 50–150ms (M1 CPU)

Stub mode:
- 항상 <1ms

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
| GGUF | 770 MB (`models/Llama-3.2-1B-Instruct-Q4_K_M.gguf`) |
| llama-cpp-python wheel | ~70 MB |
| Hook overhead per call | 1-150ms (대부분 1-5ms M13 layer) |
| RAM (LLM 메모리) | ~1.2 GB (Llama-1B Q4 + KV cache) |
| RAM (firewall idle) | ~50 MB |

8GB Mac mini 도 무리 없음. 4GB 머신에서는 `qwen-0.5b` 권장 (~600 MB RAM).

---

## 10. 다음 단계

- 하루 사용 후 `aegis report -v --since 1d` → tier 사용 분포 확인
- `--model phi-3.5-mini` 로 업그레이드 (정확도 vs 디스크)
- 알려진 P1 follow-up: Llama-1B 가 1-of-N false-negative 패턴 (AWS
  secret in `Edit` args 등) — M13 의 `tool_arg_inspection` weight 보정 예정
