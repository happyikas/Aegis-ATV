# Step340 RAG — 사례 메모리로 sLLM 보강 가이드

> Llama-3.2-1B 단독은 작아서 reasoning 능력이 약함. BGE 임베딩으로
> **유사 과거 사례 N개** 를 retrieve 해서 LLM prompt 에 주입.
> Patent step340 RAG 의 1차 구현.
>
> v0.5+: 아래 모든 `aegis case-memory <action>` 명령은 새 vocab
> `aegis coach case-memory <action>` 와 동일하게 동작합니다 (이전
> 이름은 alias 로 유지).

---

## 1. 왜 필요한가

PR #21 / #22 에서 검증된 한계:

```
Llama-3.2-1B 단독 정확도 (4-case test): 1/4
```

1B 모델은 **reasoning 은 못 해도 pattern-match 는 잘함.** 비슷한 상황의
verdict 를 보여주면 따라함. RAG 가 그 패턴을 주입하는 메커니즘.

PR #22 의 BGE 임베딩이 깔려있으면 (패턴 군집을 만들 수 있으면) 자연스럽게
다음 단계.

---

## 2. 셋업 (1 명령어)

PR #20–#22 의 셋업이 끝났다는 가정 (BGE GGUF + llama-cpp 설치).

```bash
uv run aegis case-memory build
```

생성물: `models/case_memory_v1.npz` — 245개 합성 케이스의 BGE-base-en
임베딩 (768-D) + 라벨 + reason. ~650KB.

빌드 시간: ~30초 (245번 BGE inference, M1 CPU).

---

## 3. 검증

```bash
uv run aegis case-memory status
```

출력 예시:

```
[case-memory] file:  …/models/case_memory_v1.npz
[case-memory] n:     245
[case-memory] dim:   768
[case-memory] meta:  {'built_at': '...', 'n': 245, 'source': 'synthetic', ...}
  labels:
    ALLOW                 35
    BLOCK                105
    REQUIRE_APPROVAL     105
```

dogfood 스크립트 `[11]` 라인 — 시맨틱 retrieval 검증:

```bash
./scripts/dogfood_check.sh --hybrid
```

```
[11] Step340 RAG retrieval
  ✓ memory n=245, retrieved 3 (top cos=0.980, label=BLOCK, 3/3 malicious)
✓ 10/10 checks passed
```

`top cos=0.980` 은 destructive query 에 대해 가장 비슷한 저장 케이스가
99% 코사인 유사도. `3/3 malicious` = 전부 BLOCK/REQUIRE_APPROVAL — 진짜
의미 있는 retrieval.

---

## 4. RAG 가 LLM prompt 에 어떻게 주입되는가

기존 prompt:
```
You are AegisData's local sLLM judge.
Rules: ...
JSON format: ...

Tool call to classify:
  summary: tool=Edit args={...}
  top M13 attribution: tool_arg_inspection: 0.30, ...

Respond with one line of JSON. JSON:
```

RAG 활성 시:
```
You are AegisData's local sLLM judge.
Rules: ...
Similar past cases (most-similar first):
- [cos=0.79] adding cloud credentials inline | tool=Edit args={...} → BLOCK (credential_leak)
- [cos=0.79] adding cloud credentials inline | tool=Edit args={...} → BLOCK (credential_leak)
- [cos=0.78] adding cloud credentials inline | tool=Edit args={...} → BLOCK (credential_leak)
JSON format: ...

Tool call to classify:
  summary: tool=Edit args={...}
  ...
```

LLM 이 "비슷한 과거 호출 3개가 모두 BLOCK 이었다" 는 strong prior 를
받음.

---

## 5. RAG 가 활성되는 조건 (자동, 명시적 토글 없음)

`local_phi.py:_build_rag_block()` 의 결정 트리:

| 조건 | RAG 활성? |
|---|:---:|
| `AEGIS_EMBEDDING_PROVIDER=bge-local` 안 됨 | ❌ (cosine 의미 없음) |
| `models/case_memory_v1.npz` 없음 | ❌ |
| ATV vector 가 `None` (text-only call) | ❌ |
| 위 3가지 다 만족 | ✅ |

→ 사용자가 PR #22 수준 셋업까지 다 했으면 자동 활성. 별도 install 플래그
없음. RAG 가 어떤 이유로든 실패하면 *조용히* fallback (no-RAG prompt) —
firewall 절대 막지 않음.

---

## 6. 진짜 Burn-in Shadow 데이터로 RAG 강화

합성 245개의 한계 (PR #23 의 M13 데이터와 동일):
- 작성자가 상상한 7개 카테고리만 커버
- 실제 사용자의 워크플로우 분포와 불일치 가능

**Shadow phase 데이터 기반 retrain:**

```bash
# 1. Shadow 모드 켜고 한 달 사용
export AEGIS_BURNIN_SHADOW=1

# 2. 데이터 모이면 case memory 재구축
uv run aegis case-memory import --corpus ~/.aegis/shadow.jsonl

# 3. 검증
uv run aegis case-memory status      # n 확인
./scripts/dogfood_check.sh           # [11] PASS 확인
```

기존 `case_memory_v1.npz` 가 덮어쓰여짐. 옛 메모리 보존하려면
`--out models/case_memory_v2_shadow.npz` 로 저장 후 수동 swap.

---

## 7. 정직한 한계

검증 데이터 (1 case 직접 테스트, AWS_SECRET_ACCESS_KEY in Edit):

| 단계 | 결과 |
|---|---|
| RAG retrieval | ✅ 3/3 케이스 모두 비슷 (cos=0.79), 모두 BLOCK 라벨 |
| Llama-1B verdict | ❌ ALLOW (RAG context 무시) |

**Llama-1B 의 fundamental limitation.** RAG context 를 주는 것만으로는
1B 모델이 always 따르지 않음. 그런데 hybrid cascade 가 보호:
- M13 attribution head (Tier 1) 이 AWS secret 을 이미 0.95 score 로
  잡아서 LLM 까지 안 감

**RAG 의 진짜 값:**

1. **Phi-3.5-mini (2.2GB) 사용자** — 강한 instruction-following 으로
   RAG context 를 따름. `aegis pull-model --model phi-3.5-mini` 후
   상당한 정확도 향상 기대.
2. **감사 trail** — prompt 에 "어떤 과거 사례를 고려했는지" 가 기록됨.
   step340 의 explainability 강화.
3. **Shadow phase 데이터 누적 후** — 실제 사용자 패턴이 메모리에 들어가면
   1B 모델도 "내가 본 적 있는 패턴" 으로 인식해서 follow.
4. **borderline cases** — M13 score 가 그레이존 (0.40-0.55) 일 때 LLM
   에게 escalate 됨. 이때 RAG 가 결정적.

---

## 8. 누적 4개 PR 의 Solo Free 인프라

```
PR #20  install plumbing (PostToolUse + venv python + --judge)
PR #21  진짜 sLLM judge — Llama-3.2-1B Q4 GGUF
PR #22  진짜 embedding — BGE-base-en-v1.5 GGUF
PR #23  M13 v2 weight 학습 — Burn-in Shadow + synthetic seed
PR #24  step340 RAG — case memory + nearest-neighbour retrieval
```

dogfood: **10/10 PASS.** 회귀: **7/7 PASS.**

Patent claim 약속의 거의 전체 surface 가 Solo Free 사용자 한 명에게
free 로 deliver 됨.

---

## 9. 다음 트랙 후보

| Track | 효과 | 메모 |
|---|---|---|
| `session_behavioral_drift` | 새 attack class 탐지 | 세션 시작 vs 현재 의미 거리 |
| `aegis uninstall` | UX | settings.json 자동 cleanup |
| Audit log rotation | 운영 | logrotate / size-based |
| Phi-3.5-mini 업그레이드 가이드 | 정확도 | 2.2GB 다운 + RAG follow 검증 |
| Hybrid M13 threshold 추가 calibration | mundane Bash false-positive 감소 | Shadow 데이터 모인 후 |
