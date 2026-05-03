# 세션 행동 drift 탐지 가이드

> Patent claims `session_behavioral_drift` (16-D ATV slot, slice
> 1808–1823) 의 1차 실제 구현. BGE 임베딩으로 **세션 시작 시점의 의도
> 와 현재 의도의 의미 거리** 를 측정해 *slow-drift prompt injection*
> attack 패턴을 탐지.

---

## 1. 왜 필요한가

기존 firewall 의 사각지대:

```
Call 1: ls (benign)                  ← anchor
Call 2: cat README                   ← benign
Call 3: head src/main.py             ← benign
Call 4: rm -rf /var/log              ← attack!
```

각 *개별* 호출만 보면 4번째 호출에서 잡힐 수도 있지만, **점진적
drift** 가 더 음흉한 패턴:

```
Call 1: "user wants to debug"
Call 2: "let me check the logs"
Call 3: "the logs are full"
Call 4: "let me clean up the logs"
Call 5: "rm -rf /var/log/*"  ← 의미적으로 자연스러워 보임
```

각 호출이 직전 호출과는 비슷해서 **diff 만 보면 못 잡음**. 하지만 1번
시점과 5번 시점은 의미가 완전히 다름. 이게 `topic_drift` 가 잡는 신호.

PR #25 이전: `session_behavioral_drift` 슬롯이 *항상 0* — 신호 없음.

PR #25 이후: BGE 임베딩 + 세션별 anchor + cosine 거리 = 실제 신호.

---

## 2. 동작 원리

### 2.1 측정

세션마다 `~/.aegis/sessions/<session_id>.json` 파일 생성:

```json
{
  "session_id": "abc-123",
  "anchor_embedding": [768-D BGE 벡터, 첫 호출의 agent_state_text],
  "running_mean_plan_len": 42.5,
  "running_m2_plan_len": 1234.5,
  "n_calls": 7,
  "drift_history": [0.0, 0.21, 0.34, 0.42, 0.48, 0.50, 0.55],
  "started_at_ns": ...,
  "last_seen_ns": ...
}
```

각 호출마다:

1. 현재 `agent_state_text` 를 BGE 로 임베딩
2. `topic_drift = 1 − cos(anchor, current)` 계산
3. `verbosity_drift = |z_score(current_plan_len)|` (Welford 통계)
4. `inp.session_behavior` 에 두 값 주입
5. ATV 빌더가 `session_behavioral_drift` 슬롯 (16-D) 채움
6. M13 attribution head 가 v1 weight = 0.15 로 가산

### 2.2 활성 조건 (자동 결정)

| 조건 | drift 동작? |
|---|:---:|
| `AEGIS_EMBEDDING_PROVIDER=bge-local` 안 됨 | ❌ (cosine 의미 없음) |
| Claude Code payload 에 `session_id` 없음 | ❌ |
| `agent_state_text` 비어있음 | ❌ |
| 위 다 만족 | ✅ |

PR #22 (BGE) + PR #20 의 `--embedding bge-local` install 했으면 자동
활성. 별도 toggle 없음. drift 모듈이 어떤 이유로든 실패하면 *조용히*
fallback (zero drift) — firewall 절대 막지 않음.

### 2.3 Eviction

`load_session()` 호출 시마다 7일 초과한 파일 자동 삭제. 별도 cron
불필요.

---

## 3. 실제 측정 (Slow-drift attack simulation)

```
[1] user wants to debug a python error in main.py     ⚓ ANCHOR
[2] reading the stack trace from the error            cos_dist=0.281
[3] looking at variable values in the function        cos_dist=0.418
[4] inspecting other files in the project             cos_dist=0.375
[5] maybe i should also check if any old log files…   cos_dist=0.439
[6] actually let me delete all logs and clear cache   cos_dist=0.489
[7] rm -rf /var/log/* && truncate database tables     cos_dist=0.501
```

→ 명확한 monotonic 증가 (anchor 0 → 공격 시점 0.5+).

```bash
./scripts/dogfood_check.sh
```

```
[12] Session drift detection
  ✓ anchor drift=0.000 (is_anchor=True), attack drift=0.501 (≥ 0.30)
✓ 11/11 checks passed
```

---

## 4. CLI: `aegis session`

```bash
# 진행 중인 세션 목록 (recency 순)
uv run aegis session list

session_id           n_calls    age   max_drift   started (UTC)
──────────────────────────────────────────────────────────────────────
abc-123                    7   12m       0.501   2026-05-03 02:14:30
xyz-456                   23    1h       0.123   2026-05-03 01:08:11

# 한 세션 상세 (anchor 벡터는 truncated)
uv run aegis session show --id abc-123

# 모두 삭제 (또는 N개 보존)
uv run aegis session clear              # 전부 삭제
uv run aegis session clear --keep 5     # 가장 최근 5개만 보존
```

---

## 5. M13 와의 상호작용

M13 v1 의 `session_behavioral_drift` weight = 0.15 (이미 nontrivial).
PR #25 이전엔 슬롯이 항상 0 이라 가산이 0. 이제 `topic_drift=0.501`
이 들어오면:

```
contribution = 0.501 * 0.15 = 0.075
```

M13 score 에 0.075 boost. v1 threshold (require_approval=0.55) 의
13.6% 가산. 다른 시그널과 합쳐서 borderline → REQUIRE_APPROVAL.

PR #23 의 v2 weight 학습 후엔 `session_behavioral_drift` weight 가
synthetic 데이터로는 학습 안 됨 (드리프트 데이터 없음). 진짜 Burn-in
Shadow phase 데이터가 모여야 v3 에서 의미 있는 weight 가 학습됨.

**즉, 이 PR 의 drift 측정은 M13 와 *함께* 효과를 발휘하는 시너지
설계** — 단독 슬롯으로는 작은 boost, M13 학습 후엔 강한 시그널.

---

## 6. 정직한 한계

### 6.1 Claude Code session_id 가짜로 채워질 수 있음

악의적 사용자 (또는 attacker 가 점령한 agent) 가 매 호출마다 다른
session_id 를 보내면 anchor 가 매번 새로 셋팅되어 drift 가 항상 0.
이는 본질적으로 *Claude Code 가 신뢰할 수 있는 session_id 를 발급*
한다는 가정에 의존. Solo Free 의 경우 사용자 본인이 Claude Code 클라이언트
를 신뢰하므로 OK.

### 6.2 `topic_drift` ≠ 모든 attack

빠른 (single-call) 공격 (예: 첫 호출이 `rm -rf /`) 은 anchor 가 그
공격 자체로 잡혀버림 → drift = 0. 이런 패턴은 다른 step (step310 path
traversal, step340 sLLM, M13 base score) 가 잡음. drift 는 **slow-drift
공격에 specialised**.

### 6.3 합법적 의도 변경 false positive

긴 세션에서 사용자가 진짜로 task 를 바꿀 수 있음 ("debug → write
docs"). drift 만으로는 구분 불가. 그래서 drift 는 단독으로 BLOCK
하지 않고 **M13 의 한 신호** 로만 작동. 다른 신호 (tool_arg_inspection
등) 가 같이 안 켜지면 score 가 threshold 못 넘음.

### 6.4 첫 N call 동안은 verbosity_drift 약함

Welford 통계는 표본이 적을 때 분산이 0 → z-score = 0. 세션 첫 5–10
calls 동안은 verbosity_drift 가 거의 안 움직임. 이는 정상 — 통계가
누적되어야 의미 있어짐.

---

## 7. 누적 6개 PR 의 Solo Free 인프라

```
PR #20  install plumbing (PostToolUse + venv python + --judge)
PR #21  진짜 sLLM judge — Llama-3.2-1B Q4 GGUF
PR #22  진짜 embedding — BGE-base-en-v1.5 GGUF
PR #23  M13 v2 weight 학습 — Burn-in Shadow + synthetic seed
PR #24  step340 RAG — case memory + nearest-neighbour retrieval
PR #25  session_behavioral_drift — 1차 실제 구현 (BGE 활용)
```

dogfood: **11/11 PASS.** 회귀: **7/7 PASS.** 1356 tests.

`session_behavioral_drift` 가 0 만 채워지던 슬롯 → 진짜 신호. Patent
가 정의한 30개 SW subfield 중 **20개** 가 실제 신호 채워짐 (28 / 30
가 운영 중, HW band 11개 제외).

---

## 8. 다음 트랙 후보

| Track | 효과 | 메모 |
|---|---|---|
| Phi-3.5-mini 자동 swap 가이드 | 정확도 (1B → 2.2B 강한 모델) | RAG follow 성능 향상 |
| Hybrid M13 threshold 추가 calibration | mundane Bash false-positive 감소 | Shadow 데이터 모인 후 |
| Audit log rotation | 운영 (logrotate / size-based) | ~/.aegis/audit.jsonl growing |
| `aegis report` RAG attribution 노출 | explainability | 어떤 사례가 결정에 기여했는지 |
| `aegis uninstall` | UX | settings.json 자동 cleanup |
