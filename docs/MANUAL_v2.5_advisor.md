# AegisData v2.5 사용자 매뉴얼 — ActionAdvice (PR-ζ-head + PR-ψ-gating)

**대상:** Claude Code (local 모드) 사용자
**최종 갱신:** 2026-05-05
**버전:** v2.5.1 — sLLM-backed Advisor + Critical-Moment Gating (Phase C)
**한 줄:** "방화벽이 *중요한 순간에만* sLLM 을 불러서 *다음에 무엇을 해야 하는지*를 알려준다."

이 문서는 v2.2 must-install 매뉴얼([MANUAL_v2.2.md](MANUAL_v2.2.md))을 이미 읽었거나
설치를 완료한 사용자를 위한 **증분 매뉴얼**입니다. v2.5 는 v2.2 / v2.3 / v2.4 surface
를 그대로 둔 채, PreToolUse 훅이 ActionAdvice 한 블록을 추가로 산출하도록 한
**opt-in** 변경입니다. 기본값에서는 동작이 v2.4 와 100% 동일합니다.

---

## 목차

1. [한 줄 요약](#한-줄-요약)
2. [Phase A → C 전체 그림](#phase-a--c-전체-그림)
3. [ActionAdvice 가 무엇인가](#actionadvice-가-무엇인가)
4. [Critical-Moment Gating (v2.5.1)](#critical-moment-gating-v251)
5. [켜는 법 — 환경 변수 두 개](#켜는-법--환경-변수-두-개)
6. [Claude Code 에서 직접 검증하기 — 7 가지 시나리오](#claude-code-에서-직접-검증하기--7-가지-시나리오)
7. [출력 해석 — 감사 로그와 stderr](#출력-해석--감사-로그와-stderr)
8. [내부 동작 — 4-layer narrative → sLLM](#내부-동작--4-layer-narrative--sllm)
9. [트러블슈팅](#트러블슈팅)
10. [FAQ](#faq)

---

## 한 줄 요약

```
AEGIS_ADVISOR_ENABLED=1                # 휴리스틱 (sub-ms, API 키 불필요)
AEGIS_ADVISOR_ENABLED=1 \              # sLLM 강화 (Anthropic Haiku 4.5)
  AEGIS_ADVISOR_PROVIDER=haiku
```

ALLOW / BLOCK / REQUIRE_APPROVAL 세 가지 동기 verdict 옆에,
방화벽은 이제 **ActionAdvice** 라는 다음 행동 추천 블록을 추가로 만듭니다:

```json
{
  "decision": "REQUIRE_APPROVAL",
  "reason": "1 burn-in alert(s); session_error_rate above baseline",
  "confidence": 0.85,
  "next_action_hint": "agent appears confused (recent edit-revert) — ask the user to clarify",
  "alternative_tool": "Read",
  "cited_anomalies": ["session_error_rate"],
  "cited_turns_rel": [-2, -1],
  "advisor_kind": "sllm-haiku",
  "advisor_hash": "a1b2…",
  "produced_at_ns": 1746345600123456000
}
```

차단되었을 때 stderr 에 `hint:` / `alt:` 두 줄이 추가로 나타나
사용자가 **무엇을** 해야 할지 즉시 알 수 있습니다.

---

## Phase A → C 전체 그림

| Phase | PR | 역할 | 비유 |
|---|---|---|---|
| **A** | [#59 PR-θ](https://github.com/happyikas/Aegis-ATV/pull/59) | TemporalContext (turn ring buffer) | CCTV |
| **A** | [#60 PR-ε](https://github.com/happyikas/Aegis-ATV/pull/60) | Burn-in baseline + z-score 이상치 태그 | 평소 vs 지금 |
| **A** | [#61 PR-ζ-schema](https://github.com/happyikas/Aegis-ATV/pull/61) | ActionAdvice dataclass + heuristic 컴포저 | 출력 계약 |
| **B** | [#62 PR-ι](https://github.com/happyikas/Aegis-ATV/pull/62) | k-means trajectory cluster catalog | "최근 패턴" 라벨 |
| **B** | [#63 PR-η](https://github.com/happyikas/Aegis-ATV/pull/63) | Logistic-regression intent classifier | "지금 무슨 task 인가" |
| **B** | [#64 PR-κ](https://github.com/happyikas/Aegis-ATV/pull/64) | 16-D action embedding table | "비슷한 도구가 뭐지" |
| **C** | [#65 PR-ζ-head](https://github.com/happyikas/Aegis-ATV/pull/65) | sLLM-backed advisor | 위 6개를 묶어 "다음 행동" 추천 |
| **C** | (this PR) | 훅에 advisor 배선 | 사용자 가시 surface 까지 도달 |

---

## ActionAdvice 가 무엇인가

`ActionAdvice` 는 PreToolUse 훅이 verdict 와 **함께** 산출하는 구조화된
"다음 행동 추천" 객체입니다. 스키마는 [src/aegis/judge/action_advice.py](../src/aegis/judge/action_advice.py)
에 정의되어 있고, 필드는 다음과 같습니다:

| 필드 | 타입 | 의미 |
|---|---|---|
| `decision` | ALLOW / BLOCK / REQUIRE_APPROVAL / **DEFER** | 방화벽 verdict 호환 (DEFER 는 추가) |
| `reason` | str | 인용된 anomaly / turn 을 포함한 한 줄 사유 |
| `confidence` | float [0,1] | self-reported (`__post_init__` 에서 clamp) |
| `next_action_hint` | str \| null | 짧은 명령형 — "사용자에게 의도를 다시 물어보세요" |
| `alternative_tool` | str \| null | 다른 도구 추천 — Edit 대신 Read 등 |
| `cited_anomalies` | tuple[str, …] | reason 을 뒷받침하는 metric 이름들 |
| `cited_turns_rel` | tuple[int, …] | reason 이 본 과거 turn 의 상대 인덱스 |
| `advisor_kind` | heuristic / sllm-haiku / sllm-phi3 / learned-head | 어떤 brain 이 만들었는지 |
| `advisor_hash` | str | SHA3-256(revision \|\| model \|\| system-prompt) — 감사용 핀 |
| `produced_at_ns` | int | 생성 시각 |

**왜 verdict 만으로 부족한가?**
- verdict 는 "막았다 / 통과시켰다" 의 이진 출력입니다.
- 사용자(또는 Claude 본인)는 *왜* 막혔는지뿐 아니라 *지금 다음 무엇을 해야 하는지*도 필요합니다.
- ActionAdvice 는 두 번째 질문에 답합니다.

---

## Critical-Moment Gating (v2.5.1)

**왜 게이팅이 있나?** sLLM 의 강점은 "여러 신호를 종합" 이지 "매번 ALLOW/BLOCK 도장 찍기" 가 아닙니다. routine ALLOW 마다 4-layer 파이프라인 + (선택적으로) Haiku 호출을 돌리면 비용·latency 만 올라가고 모델은 deterministic 룰과 같은 verdict 만 반복합니다. 그래서 v2.5.1 부터는 advisor 가 **"critical moment"** 에서만 발동하도록 게이트가 들어갔습니다.

### 게이트 동작 — 5 가지 신호 중 하나라도 켜지면 advisor 호출

게이트는 PreToolUse 훅이 *이미* 계산한 신호만 봅니다 (sub-microsecond). 추가 파이프라인 작업 없음.

| 번호 | 신호 | 트리거 조건 |
|---|---|---|
| 1 | `verdict.decision != "ALLOW"` | BLOCK / REQUIRE_APPROVAL / DEFER. M12 escalation 도 여기서 잡힘 |
| 2 | `aegis.cost.escalation` trace 존재 | M12 cost-divergence (defensive — 1번이 못 잡았을 때) |
| 3 | step336 trace 에 `loop` / `redundant` | 동일 호출 ≥3회 또는 read-only 중복 |
| 4 | step335 trace 에 `warn` | 누적 비용이 한도 근접 |
| 5 | step337 trace 에 `anomaly` 또는 `alert` (T2 default 제외) | HW 이상치 (`AEGIS_HW_PROVIDER=sim` / `real` 일 때만) |

이 중 어느 하나가 만족되면 **invoked: true**, 만족 안 되면 **invoked: false** 로 audit 에 기록됩니다.

### 게이트 출력 — `explain.advisor_gate`

advisor 가 **켜져 있으면**, audit 의 모든 PreToolUse 라인에 다음 블록이 추가됩니다:

```jsonc
"explain": {
  "advisor_gate": {
    "invoked": false,
    "reason": "no critical signals"        // routine ALLOW
  }
  // ↑ action_advice 키는 invoked: true 일 때만 추가됨
}
```

호출된 경우:

```jsonc
"explain": {
  "advisor_gate": {
    "invoked": true,
    "reason": "verdict=REQUIRE_APPROVAL"   // 또는 "loop/redundancy signal" 등
  },
  "action_advice": { ... }                  // 4-layer narrative 통과한 결과
}
```

### 비용·latency 영향

| 시나리오 | v2.5.0 (gate 없음) | v2.5.1 (gate) |
|---|---|---|
| Routine ALLOW (대부분) | advisor 풀 실행 ~5 ms (heuristic) / ~150 ms (haiku) | gate 평가만 ~0.01 ms |
| Critical moment (~5-10%) | 동일 | 동일 (gate 통과 후 풀 실행) |
| **시간당 호출 100 회 가정 Haiku 모드 비용** | **$0.03** | **~$0.003** (1/10) |

게이트 통과한 호출만 4-layer 파이프라인을 도므로 transcript / audit JSONL I/O 도 그만큼 줄어듭니다.

### 게이트 끄기 — `AEGIS_ADVISOR_ALWAYS=1`

burn-in 데이터 수집이나 디버깅 시 게이트를 우회하고 매 호출마다 advisor 를 돌리고 싶다면:

```bash
AEGIS_ADVISOR_ENABLED=1 \
AEGIS_ADVISOR_ALWAYS=1 \
... (기존 환경) ...
```

이 경우 `advisor_gate.reason == "AEGIS_ADVISOR_ALWAYS=1"` 로 audit 에 기록되어 나중에 gate-bypass 로 수집된 advice 를 식별할 수 있습니다.

### 왜 M13 confidence / session_drift 는 게이트에 없나?

calibration 이슈입니다. M13 attribution head 의 confidence 는 routine ALLOW 에서도 자연스럽게 0.3-0.5 가 나오는 분포라, "낮으면 critical" 이라는 룰을 그대로 쓰면 false positive 가 너무 많습니다. session_drift 도 마찬가지로 burn-in 학습 전에는 임계값이 의미 없습니다. 두 신호는 burn-in 데이터로 임계값을 학습한 후 v2.6 에서 게이트에 추가될 예정입니다.

---

## 켜는 법 — 환경 변수 두 개

기본값에서는 v2.4 와 동일하게 동작합니다 (`AEGIS_ADVISOR_ENABLED` 미설정 → advisor 비활성).
켜려면:

### A. 휴리스틱 모드 (기본 권장 — API 키 불필요, sub-ms)

```bash
# ~/.claude/settings.json 의 PreToolUse hook command 에 prepend:
AEGIS_ADVISOR_ENABLED=1 \
AEGIS_EMBEDDING_PROVIDER=dummy \
AEGIS_JUDGE_PROVIDER=dummy \
... (기존 환경) ...
python /path/to/Aegis-ATV/tools/aegis_local_hook.py
```

이 모드에서는 [src/aegis/judge/action_advice.py:compose_advice_heuristic](../src/aegis/judge/action_advice.py)
이 결정론적 룰 (≥1 alert → REQUIRE_APPROVAL, ≥2 warning → REQUIRE_APPROVAL, …)
을 사용합니다. **Anthropic 키 불필요. p99 < 1 ms.**

### B. sLLM 모드 (Haiku 4.5 — 의미 기반 추천)

```bash
AEGIS_ADVISOR_ENABLED=1 \
AEGIS_ADVISOR_PROVIDER=haiku \
ANTHROPIC_API_KEY=sk-ant-... \
python /path/to/Aegis-ATV/tools/aegis_local_hook.py
```

이 모드에서는 4-layer narrative + 16-D action embedding 의 top-3 유사 도구가
시스템 프롬프트와 함께 Haiku 4.5 에게 전달되고, 모델이 위 스키마의
**JSON 만** 응답하도록 합니다. p50 ~150 ms.

> 키가 없거나 API 가 503 을 반환하면 자동으로 휴리스틱 fallback. **방화벽 가용성은 절대 advisor 에 의존하지 않습니다.**

### 후크가 의도대로 켜졌는지 빠른 확인

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"test","tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}' \
  | AEGIS_ADVISOR_ENABLED=1 \
    AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy \
    python /path/to/Aegis-ATV/tools/aegis_local_hook.py

# 종료 코드 0 (ALLOW)
tail -1 ~/.aegis/audit.jsonl | jq '.explain.action_advice'
# {
#   "decision": "ALLOW",
#   "reason": "no anomalies; pass-through",
#   "confidence": 0.9,
#   "advisor_kind": "heuristic",
#   ...
# }
```

`action_advice` 키가 보이면 OK. 없으면 `AEGIS_ADVISOR_ENABLED=1` 이
훅 명령줄에 실제로 적용되었는지 확인하세요.

---

## Claude Code 에서 직접 검증하기 — 7 가지 시나리오

설치:

```bash
cd /path/to/Aegis-ATV
uv sync
uv run aegis install --mode local
# ~/.claude/settings.json 의 PreToolUse 명령 앞에 AEGIS_ADVISOR_ENABLED=1 추가
# Claude Code 재시작
```

각 시나리오는 (1) Claude Code 안에서 어떤 요청을 하는지, (2) 어떤 verdict
와 advice 를 기대하는지, (3) 어디서 확인할 수 있는지를 알려줍니다.

### 시나리오 1 — 무해한 Read (게이트 skip → advice 없음)

**Claude 에 요청:**
> "현재 디렉터리의 README.md 첫 30줄만 읽어줘"

**기대 (v2.5.1 gate 후):**
- exit 0 (ALLOW)
- 게이트 신호 0개 → `explain.advisor_gate.invoked == false`, `reason == "no critical signals"`
- `explain.action_advice` 키는 **없음** — 4-layer 파이프라인 자체가 안 돌았음

**확인:**
```bash
tail -1 ~/.aegis/audit.jsonl | jq '.explain | {gate: .advisor_gate, advice_present: (.action_advice != null)}'
# {
#   "gate": { "invoked": false, "reason": "no critical signals" },
#   "advice_present": false
# }
```

advisor 동작을 강제로 보고 싶으면 `AEGIS_ADVISOR_ALWAYS=1` 로 게이트 우회.

### 시나리오 2 — 차단된 destructive Bash (BLOCK + reason)

**Claude 에 요청:**
> "git push --force origin main 실행해줘"

**기대:**
- exit 2 (BLOCK)
- stderr 에 다음 형태:
  ```
  [aegis-local] BLOCK  Bash  trace=...  (Xms)
             reason: rule:git_destructive  ...
             hint:   ...                          ← advisor enabled 시
             alt:    try `Read` instead           ← advisor enabled 시
  ```
- audit 의 `decision == "BLOCK"`, `action_advice.advisor_kind in ("heuristic", "sllm-haiku")`

**확인:**
```bash
tail -1 ~/.aegis/audit.jsonl | jq '{d: .decision, advice: .explain.action_advice}'
```

### 시나리오 3 — 반복 호출 루프 (REQUIRE_APPROVAL + loop hint)

**Claude 에 요청 (의도적으로 같은 명령 3번 시키기):**
> "ls -la 를 정확히 같은 옵션으로 세 번 실행해줘"

**기대:**
- 3번째 호출에서 step336 loop detector 가 REQUIRE_APPROVAL 발생
- `action_advice.next_action_hint` 에 "same call repeated within window" 류 문구 (휴리스틱) 또는 모델이 작성한 의미적 hint (Haiku)
- `cited_turns_rel` 에 음수 인덱스들 (예: `[-2, -1, 0]`)

**확인:**
```bash
tail -3 ~/.aegis/audit.jsonl | jq '{tool, decision: .decision, reason: .reason, hint: .explain.action_advice.next_action_hint}'
```

### 시나리오 4 — Edit 후 backtrack → alt_tool=Read

**Claude 에 요청 (실수 시뮬레이션):**
> "src/aegis/judge/action_advice.py 의 임의 함수에 한 줄 추가해줘. 잠깐, 잘못 보았어. 다시 그 줄 지워줘."

**기대:**
- 두 번째 Edit 호출이 PostToolUse 의 backtrack 신호와 함께 기록됨
- 다음 PreToolUse 에서 `alternative_tool == "Read"` (Edit + backtrack 패턴 → Read 권장)
- hint 에 "agent appears confused (recent edit-revert) — ask the user to clarify" 형태

**확인:**
```bash
tail -1 ~/.aegis/audit.jsonl | jq '.explain.action_advice | {alt_tool: .alternative_tool, hint: .next_action_hint}'
```

### 시나리오 5 — 토큰 폭주 시 burn-in alert

전제: `~/.aegis/audit.jsonl` 에 burn-in 베이스라인이 학습되어 있어야 함
(`uv run aegis baseline retrain` 으로 새로 만들거나, `models/burnin_baseline_v1.json`
의 동봉 버전을 사용).

**Claude 에 요청 (긴 컨텍스트 강제):**
> "이 100k 토큰짜리 로그 전체를 한 번에 읽고 요약해줘"

**기대:**
- step335 비용 게이트나 PR-ε burn-in `window_token_velocity_per_turn` z-score 이 ≥3σ
- `cited_anomalies` 에 `window_token_velocity_per_turn` 등이 포함
- hint 가 "consider summarising context or starting a fresh session"

**확인:**
```bash
tail -1 ~/.aegis/audit.jsonl | jq '.explain.action_advice'
```

### 시나리오 6 — sLLM 모드 의미 기반 alt-tool

전제: `AEGIS_ADVISOR_PROVIDER=haiku` + `ANTHROPIC_API_KEY` 설정.

**Claude 에 요청:**
> "이 디렉터리에서 'TODO' 가 들어간 줄을 모두 찾아줘 — Read 로 한 파일씩 다 읽어보면서"

**기대:**
- Read 가 비효율적인 선택임을 모델이 알아챔 (PR-κ 의 Grep 유사도가 높음)
- `alternative_tool == "Grep"` (또는 `"Glob"`)
- hint 에 "use Grep with a pattern instead of reading every file" 형태

**확인:**
```bash
tail -1 ~/.aegis/audit.jsonl | jq '.explain.action_advice | {kind: .advisor_kind, alt: .alternative_tool, hint: .next_action_hint}'
# advisor_kind 가 "sllm-haiku" 여야 함
```

### 시나리오 7 — Advisor 가 죽어도 방화벽은 산다

**의도적으로 깨뜨리기:**
```bash
# 잘못된 모델명으로 강제
AEGIS_ADVISOR_ENABLED=1 \
AEGIS_ADVISOR_PROVIDER=haiku \
ANTHROPIC_API_KEY=invalid \
... (Claude Code 명령) ...
```

**기대:**
- 방화벽 verdict 는 정상 동작 (exit 0/2 그대로)
- audit 의 `action_advice.advisor_kind == "heuristic"` (자동 fallback)
- 어떤 경우에도 advisor 실패가 verdict 를 바꾸거나 도구 호출을 막지 않음

이게 **Hard contract**: advisor 는 best-effort bookkeeping 이며,
방화벽 hot path 와 결합되어 있지 않습니다 ([tools/aegis_local_hook.py](../tools/aegis_local_hook.py) 의 `_compute_action_advice` 는 try/except 로 감싸져 있음).

---

## 출력 해석 — 감사 로그와 stderr

### 1. `~/.aegis/audit.jsonl`

각 PreToolUse 한 줄. advisor 가 켜져 있으면 다음과 같은 모양:

```json
{
  "ts_ns": 1746345600000000000,
  "tool": "Bash",
  "aid": "sess-abc123",
  "decision": "REQUIRE_APPROVAL",
  "reason": "loop detector: same call ≥3×",
  "trace_id": "...",
  "latency_ms": 4.7,
  "mode": "local",
  "explain": {
    "atv_dim": 2080,
    "atv_sha3": "...",
    "step_traces": { ... },
    "m13_top": [ ... ],
    "action_advice": {
      "decision": "REQUIRE_APPROVAL",
      "reason": "...; same call repeated 3 times in window",
      "confidence": 0.75,
      "next_action_hint": "same call repeated within window — try a different tool",
      "alternative_tool": "Glob",
      "cited_anomalies": ["session_redundancy_ratio"],
      "cited_turns_rel": [-2, -1, 0],
      "advisor_kind": "heuristic",
      "advisor_hash": "8f3a…",
      "produced_at_ns": 1746345600000000000
    }
  }
}
```

`jq` 쿼리 예시:

```bash
# 마지막 advice 만 깔끔하게
tail -1 ~/.aegis/audit.jsonl | jq '.explain.action_advice'

# advisor_kind 분포 (heuristic vs sllm-haiku)
jq -r '.explain.action_advice.advisor_kind // empty' ~/.aegis/audit.jsonl | sort | uniq -c

# 최근 차단된 도구의 alternative_tool 들
jq -r 'select(.decision != "ALLOW") | .explain.action_advice.alternative_tool' ~/.aegis/audit.jsonl | sort | uniq -c
```

### 2. stderr (Claude Code 가 보는 것)

ALLOW 시:
```
(stderr 출력 없음, 단 AEGIS_HOOK_VERBOSE=1 일 때 ALLOW 한 줄)
```

REQUIRE_APPROVAL / BLOCK 시 (advisor 켜져 있을 때):
```
[aegis-local] REQUIRE_APPROVAL  Bash  trace=04122172  (118.3ms)
           reason: loop detector: same call ≥3×
           hint:   same call repeated within window — try a different tool
           alt:    try `Glob` instead
```

`hint:` 와 `alt:` 줄은 advisor 가 해당 필드를 채웠을 때만 나타납니다.

---

## 내부 동작 — 4-layer narrative → sLLM

advisor 는 호출당 다음을 수행합니다 ([_compute_action_advice](../tools/aegis_local_hook.py)):

```
load_recent_history(audit + transcript)              ← PR-θ TemporalContext
  ↓
load_baseline_or_default()  + compute_anomalies()    ← PR-ε z-scores
  ↓
load_catalog_or_default()                            ← PR-ι nearest k-means cluster
  ↓
load_classifier_or_default()                         ← PR-η intent softmax
  ↓
load_table_or_default()                              ← PR-κ 16-D embeddings
  ↓
compose_advice_sllm(temporal_ctx, anomalies, baseline,
                    catalog, intent_classifier, action_table,
                    base_decision, base_reason, current_tool)
  ↓
{ DummyAdvisor(heuristic)   if AEGIS_ADVISOR_PROVIDER=dummy
  HaikuAdvisor              if AEGIS_ADVISOR_PROVIDER=haiku  }
  ↓
ActionAdvice (위 스키마)
  ↓
audit.jsonl ← explain.action_advice
stderr      ← hint / alt 줄 추가
```

Haiku 모드의 시스템 프롬프트는 [src/aegis/judge/advisor.py:ADVISOR_SYSTEM_PROMPT](../src/aegis/judge/advisor.py)
에 있고, 사용자 메시지는 다음 섹션을 차례로 포함합니다:

```
TEMPORAL TRAJECTORY (last N of M requested)
  · -3  Read   ok  in=120  out=300  cum=420  cache=33%
  · -2  Edit   ok  in=200  out=500  cum=1120 ↩BACKTRACK
  · -1  Edit   ok  in=180  out=450  cum=1750
  ·  0  Bash   ?   in=50   out=120  cum=1920

TRAJECTORY METRICS
  cumulative_tokens: 1920
  cache_hit_rate:    0.31
  token_velocity:    480/turn
  ...

ANOMALIES vs BURN-IN  (when baseline usable)
  ⚠ [warning] cache_hit_rate_max_drop_pp=42 (2.3σ above baseline)

NEAREST BURN-IN CLUSTERS
  edit-flow            cos=0.81
  debug-error-spiral   cos=0.62
  ...

TASK INTENT PREDICTION
  edit       0.62  ←
  debug      0.21
  refactor   0.10
  ...

CANDIDATE ALTERNATIVES (semantic similarity):
  Read       cos=0.76
  Grep       cos=0.41
  Glob       cos=0.35

PROPOSED CALL
  tool: Bash
BASE VERDICT (from firewall)
  decision: ALLOW
  reason:   (none)
```

---

## 트러블슈팅

### `action_advice` 가 audit 에 안 나타남

체크리스트:
1. `AEGIS_ADVISOR_ENABLED=1` 이 훅 명령줄에 prepend 되어 있는가?
   ```bash
   cat ~/.claude/settings.json | jq '.hooks.PreToolUse'
   ```
2. 훅 재시작 (Claude Code 재시작) 후에 시도했는가?
3. `AEGIS_HOOK_VERBOSE=1` 로 실행하면 advisor 실패 사유가 stderr 에 보임:
   ```
   [aegis-local] advisor skipped: <error>
   ```

### `advisor_kind == "heuristic"` 인데 haiku 를 원함

원인:
- `ANTHROPIC_API_KEY` 가 비어 있으면 `get_advisor()` 가 자동으로 dummy 로 fallback. 키 확인:
  ```bash
  printenv ANTHROPIC_API_KEY | head -c 10  # "sk-ant-..." 시작해야 함
  ```
- `AEGIS_ADVISOR_PROVIDER` 가 `haiku` (소문자) 인지 확인. 오타 (`Haiku`, `haku` 등) 는 dummy 로 fallback.
- API 가 503/4xx 를 반환했을 가능성. `~/.aegis/audit.jsonl` 의 latency 가 평소보다 짧으면 (< 50ms) API 호출이 실패한 것.

### Latency 가 갑자기 증가

p50 기준:
- Heuristic: < 5 ms
- Haiku: ~150 ms

Haiku 모드에서 갑자기 1초 이상 걸린다면 Anthropic API 의 일시적 지연. `AEGIS_ADVISOR_PROVIDER=dummy` 로 일시 전환하거나, advisor 를 끄면 zero-impact 로 복귀.

### Advisor 가 너무 자주 BLOCK 으로 escalate

`compose_advice_heuristic` 의 룰:
- ≥1 `alert` → REQUIRE_APPROVAL @ 0.85
- ≥2 `warning` → REQUIRE_APPROVAL @ 0.75
- 1 `warning` → 그대로 통과 @ 0.70

거짓 양성이 많다면 burn-in baseline 이 너무 좁게 학습되었을 가능성.
재학습:

```bash
uv run aegis baseline retrain --from ~/.aegis/audit.jsonl --min-sessions 20
```

---

## FAQ

**Q. v2.4 와 동시에 쓸 수 있나?**
A. v2.5 는 v2.4 의 super-set 입니다. `AEGIS_ADVISOR_ENABLED` 미설정 → v2.4 와 100% 동일 동작.

**Q. Haiku 호출 비용은?**
A. 호출당 ~$0.0003 (입력 ~2k 토큰, 출력 ~300 토큰 기준 Haiku 4.5 가격표). 100 호출 / 시간 → 시간당 $0.03 수준. cost 게이트 (`step335`) 가 advisor 호출 자체도 cumulative_dollars 에 더하므로 budget cap 안에서 보호됨.

**Q. Haiku 응답이 망가지면?**
A. `_parse_advice_json` 이 None 을 반환하면 휴리스틱 fallback (단 advisor_kind 는 sllm-haiku 로 stamp 되어 감사용 추적 가능). `decision` 은 보수적으로 REQUIRE_APPROVAL 로 격상.

**Q. `advisor_hash` 가 무엇을 보장하나?**
A. SHA3-256(revision || model || system-prompt). 동일 prompt + 동일 model 의 모든 advice 는 같은 hash 를 갖습니다. 감사 체인 replay 시 prompt 가 바뀐 advice 를 식별 가능.

**Q. 결정론이 보장되는가?**
A. **Heuristic** 모드: 100% 결정론. **Haiku** 모드: temperature 0 + 동일 입력 → 거의 동일 출력 (Anthropic 의 "stable" determinism 수준). 두 모드 모두 audit chain 의 SHA3 핀과 호환.

**Q. PreToolUse 가 아닌 PostToolUse 에도 advisor 가 있나?**
A. v2.5 시점에는 PreToolUse 만. PostToolUse 의 retrospective advice 는 후속 PR 후보 (현재 #45 의 backtrack/redundant/error 신호가 다음 PreToolUse 의 advisor 입력으로 자연스럽게 흘러들어감).

**Q. advisor 출력을 비활성화하지 않고 stderr 노이즈만 줄이고 싶다.**
A. ALLOW 시에는 hint/alt 가 stderr 에 나가지 않습니다. REQUIRE_APPROVAL / BLOCK 시에만 추가되므로, 차단 횟수 자체가 적다면 노이즈도 적습니다. 완전히 끄려면 `AEGIS_ADVISOR_ENABLED=0` (또는 미설정).

---

## 부록 — 환경 변수 한 줄 요약

| 변수 | 기본값 | 의미 |
|---|---|---|
| `AEGIS_ADVISOR_ENABLED` | (미설정 / 0) | `1` 로 설정 시 advisor 활성. `explain.advisor_gate` + (게이트 통과 시) `explain.action_advice` 가 audit 에 추가 |
| `AEGIS_ADVISOR_PROVIDER` | `dummy` | `dummy` (heuristic) 또는 `haiku` (sLLM) |
| `AEGIS_ADVISOR_ALWAYS` | (미설정 / 0) | `1` → critical-moment 게이트 우회. 매 호출마다 advisor 실행 (burn-in / 디버그용) |
| `ANTHROPIC_API_KEY` | (미설정) | `haiku` 모드에서 필수. 없으면 자동 dummy fallback |
| `AEGIS_HOOK_VERBOSE` | `0` | `1` → ALLOW 도 stderr 출력 + advisor 실패 사유 출력 |

전체 환경 변수 목록은 [docs/MANUAL_v2.2.md](MANUAL_v2.2.md) 의 "환경 변수 / 설정" 섹션 참조.
