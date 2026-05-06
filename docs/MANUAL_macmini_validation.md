# Aegis Mac mini Validation Suite — 사용 매뉴얼

**대상**: Mac mini (또는 macOS / Linux 일반) 에서 Aegis 의 v2.8 ActionStep
surface 가 기대대로 동작하는지 90개 케이스로 결정적 검증을 수행하려는
사용자.
**최종 갱신**: 2026-05-06
**버전**: 1.0.0 (PR #85)
**한 줄**: "API 키도 GPU 도 Docker 도 없이, `uv run python -m demo.macmini`
한 줄로 90개 케이스를 다 돌려서 8 advisor · 11 verb 의 동작을 입증한다."

관련 문서:

- [`docs/MANUAL_v2.2.md`](MANUAL_v2.2.md) — 풀 사용자 매뉴얼 (sidecar / local 설치)
- [`docs/MANUAL_MACMINI.md`](MANUAL_MACMINI.md) — Plugin Solo Free 모드 부트스트랩 (이번 suite와 별개 주제)
- [`docs/MANUAL_v2.5_advisor.md`](MANUAL_v2.5_advisor.md) — Advisor 카탈로그 / verb 명세

---

## 목차

1. [3분 만에 90개 케이스 돌리기](#3분-만에-90개-케이스-돌리기)
2. [무엇을 검증하는가](#무엇을-검증하는가)
3. [패키지 구조](#패키지-구조)
4. [CLI 레퍼런스](#cli-레퍼런스)
5. [출력 해석](#출력-해석)
6. [분야별 케이스 분류](#분야별-케이스-분류)
7. [환경 변수](#환경-변수)
8. [새 케이스 추가하기](#새-케이스-추가하기)
9. [CI / 자동화 통합](#ci--자동화-통합)
10. [트러블슈팅](#트러블슈팅)
11. [FAQ](#faq)
12. [부록 — 명령어 한 줄 요약](#부록--명령어-한-줄-요약)

---

## 3분 만에 90개 케이스 돌리기

```bash
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
uv sync                                       # 의존성 설치 (~30초)

uv run python -m demo.macmini all             # 90개 케이스 전체 (~10초)
```

기대 헤드라인:

```
══════════════════════════════════════════════════════════════════════════════
  Aegis Mac mini validation — summary
══════════════════════════════════════════════════════════════════════════════
  Total: 90    Pass: 90    Fail: 0    Pass-rate: 100%

  By category
    Category        Cases   Pass   Fail   Pass%
    cost               30     30      0    100%
    performance        30     30      0    100%
    security           30     30      0    100%
```

마크다운 리포트는 `docs/MACMINI_VALIDATION_REPORT.md` 에 자동 생성.
Exit code 는 0 (전부 pass) 또는 1 (한 건이라도 fail).

> **Mac mini 에서 권장**: 첫 실행 후 `--no-cases` 옵션으로 헤드라인만
> 다시 돌려보면 cache hit 으로 5초 미만에 끝납니다.

---

## 무엇을 검증하는가

이 suite 는 v2.8 ActionStep surface 의 **결정적 (deterministic)** 동작을
세 분야 × 30 = 90개 케이스로 입증합니다.

| 분야 | 검증 대상 advisor | 주요 verb |
|---|---|---|
| **Cost** | `cost-optimizer`, `kv-cache-optimizer` | `swap-model`, `prune-turns`, `end-session`, `notify-operator`, `summarize-window` |
| **Performance** | `loop-breaker`, `context-compactor`, `test-runner`, `human-clarifier` | `swap-tool`, `narrow-scope`, `summarize-window`, `clarify-intent`, `run-diagnostic` |
| **Security** | `security-reviewer`, `permission-escalator` | `require-approval`, `notify-operator` |

각 케이스는 다음 세 정보를 출력:

1. **SCENARIO** — 어떤 상황을 가정하는지 (영문 자연어 한두 줄).
2. **EXECUTION** — 실제로 무엇을 호출하는지
   (예: `compose_advice_heuristic(temporal_ctx=ctx_5turns_expensive())`,
   PreToolUse Bash 호출 등).
3. **RESULT** — pass / fail · decision · 발화한 advisor · top action_step
   · 매치 실패 시 misses 목록.

검증 모드 두 가지:

- **unit** — `compose_advice_heuristic()` 을 ATV `TemporalContext` /
  signal dict 으로 직접 호출. Cost / Performance 의 대부분이 이쪽.
- **e2e** — 실제 PreToolUse hook (`tools/aegis_local_hook.py`) 을 통해
  step310 → step311 → step312 → step320 → step330 → step335 → step336 →
  step340 파이프라인 전체를 통과시키고, audit JSONL 에 기록된
  `action_advice.recommended_advisors[]` 를 다시 읽어 비교.
  Security 의 대부분이 이쪽.

> **왜 fine-tuning 없이 가능한가** — 11-verb closed catalog 와 verb-별
> 필수 파라미터 schema 가 이미 코드에 박혀 있어, sLLM 이 emit 하는
> JSON 이 schema 를 벗어나면 defensive parser 가 silently drop 합니다.
> 따라서 *어느 brain 을 쓰든* 같은 schema 에서 결과가 나오고, 본 suite
> 는 dummy provider 로 그 결정성을 입증합니다.

---

## 패키지 구조

```
demo/macmini/
├── __init__.py              패키지 버전 + module list
├── __main__.py              CLI: python -m demo.macmini …
├── case.py                  TestCase / TestResult dataclass + check()
├── fixtures.py              ATV TemporalContext 빌더 + 토큰-분리 destructive cmd 빌더
├── runner.py                setup_environment() + e2e/unit 분기 + run_case()
├── render.py                터미널 (ANSI) + Markdown 렌더링
├── cost.py                  30 cost 케이스
├── performance.py           30 performance 케이스
└── security.py              30 security 케이스
```

각 케이스 파일은 `cases() -> list[TestCase]` 한 함수만 export.
`runner.py` 가 카테고리에 맞춰 dispatch.

---

## CLI 레퍼런스

```bash
uv run python -m demo.macmini [CATEGORY] [OPTIONS]
```

| 인자 / 옵션 | 의미 | 기본값 |
|---|---|---|
| `CATEGORY` | `cost` / `performance` / `security` / `all` | `all` |
| `--no-color` | ANSI 색상 끄기 (CI / 로그용) | off |
| `--no-cases` | 케이스별 상세 블록 생략, summary 만 출력 | off |
| `--report PATH` | Markdown 리포트 출력 경로 | `docs/MACMINI_VALIDATION_REPORT.md` |
| `--width N` | 터미널 출력 폭 | 78 |

### 자주 쓰는 조합

```bash
# 1) 전체 90개, 케이스별 상세 보기
uv run python -m demo.macmini all

# 2) Cost 만 빠르게 (summary)
uv run python -m demo.macmini cost --no-cases

# 3) CI 로그용 (색 X · 상세 X)
uv run python -m demo.macmini all --no-color --no-cases

# 4) 리포트 경로 변경
uv run python -m demo.macmini all --report /tmp/report.md
```

### Exit code

| Code | 의미 |
|---|---|
| 0 | 모든 케이스 pass |
| 1 | 한 건 이상 fail |

CI 에서 `set -e` 하에 그대로 사용 가능.

---

## 출력 해석

### 케이스 블록

```
══════════════════════════════════════════════════════════════════════════════
COST-04  │  Budget at 0.9x — exactly at threshold
──────────────────────────────────────────────────────────────────────────────
SCENARIO
  Budget at 90% triggers cost-optimizer with prune-turns as the
  lowest-friction first action.

EXECUTION
  cost_signals={'budget_used_ratio': 0.9}

RESULT   ✓ PASS
  decision      = 'ALLOW'  (0.0 ms)
  advisors      = 1 firing
    - cost-optimizer  prio=medium  verbs=prune-turns
      step: prune-turns(turn_indices_rel=[0, -1, -2], saved_tokens_estimate=15000)
══════════════════════════════════════════════════════════════════════════════
```

| 필드 | 설명 |
|---|---|
| `COST-04` | 케이스 ID. 카테고리(`COST/PERF/SEC`)+번호. |
| `decision` | `compose_advice_heuristic()` 또는 audit 의 최종 결정 (`ALLOW` / `BLOCK` / `REQUIRE_APPROVAL`). |
| `(0.0 ms)` | 케이스 단일 실행 시간. unit 은 sub-millisecond, e2e 는 100~200ms. |
| `advisors = N firing` | 발화한 advisor 수. |
| `prio=medium` | advisor 의 우선순위 (`high` / `medium` / `low`). |
| `verbs=…` | 그 advisor 가 emit 한 ActionStep verb 들 (콤마 구분). |
| `step: verb(k=v, …)` | 첫 ActionStep 의 verb + 상위 2개 파라미터. |

### Summary 블록

```
  By category
    Category        Cases   Pass   Fail   Pass%
    cost               30     30      0    100%
    performance        30     30      0    100%
    security           30     30      0    100%

  Advisor frequency (across all results)
    security-reviewer       30
    cost-optimizer          18
    loop-breaker            13
    …

  Verb frequency (across all action_steps)
    require-approval        27
    prune-turns             17
    swap-tool               13
    …
```

| 섹션 | 해석 |
|---|---|
| `By category` | 카테고리별 pass / fail. CI gate 의 1차 지표. |
| `Advisor frequency` | 90개 결과에 걸쳐 어느 advisor 가 몇 번 발화했나. **8개 advisor 가 모두 한 번 이상 등장**해야 정상. |
| `Verb frequency` | 11개 verb 중 실제로 사용된 verb 와 빈도. 10/11 이상이 정상 (`verify-state` 는 본 suite 가 의도적으로 미커버 — Sidecar M14 quorum 케이스 전용). |

### 실패 시

```
  Failures
    PERF-25    different params (e2e) — no loop                
      - decision 'REQUIRE_APPROVAL' != 'ALLOW'
      - loop-breaker unexpectedly fired
```

각 실패는 `expected vs actual` 형태 한두 줄. `--no-cases` 모드에서도
실패는 항상 출력됩니다.

---

## 분야별 케이스 분류

### Cost (30)

| ID 범위 | 그룹 | 검증 내용 |
|---|---|---|
| COST-01 | 컨트롤 | 유휴 세션 — advisor 0 발화 |
| COST-02 | flag | `budget_warn_flag` 단독으로 cost-optimizer 발화 |
| COST-03 ~ COST-07 | 임계값 | `budget_used_ratio` 0.85 / 0.9 / 1.0 / 1.5 / 2.0 boundary |
| COST-08 ~ COST-11 | M12 | `hw_vs_sw_divergence_ratio` 1.99 / 2.0 / 3.15 / 5.0 |
| COST-12 | 모델 | Sonnet → Haiku swap 경로 |
| COST-13 ~ COST-15 | 캐시 | `cache_hit_rate_max_drop_pp` 51 / 25(boundary) / prefix unstable |
| COST-16 ~ COST-21 | 조합 | cost+cache, cost+M12+velocity, 3-domain canonical, cache+backtrack |
| COST-22 ~ COST-25 | e2e ALLOW | Read /tmp / Bash echo / Grep / 작은 Edit |
| COST-26 ~ COST-30 | 추가 | 75pp 캐시 catastrophe, empty signals, security-only no-cost-fire, 4-advisor mega |

### Performance (30)

| ID 범위 | 그룹 | 검증 내용 |
|---|---|---|
| PERF-01 ~ PERF-04 | 루프 | `loop-breaker` 모든 swap 페어 (Read→Grep, Bash→Glob, Edit→Read, Grep→Glob) |
| PERF-05 | 추가 verb | loop-breaker 의 narrow-scope verb |
| PERF-06 ~ PERF-08 | 에러 | `n_errors` 1 / 2 / anomaly 태그 |
| PERF-09 ~ PERF-11 | backtrack | n_backtracks 0 / 1 / 5 |
| PERF-12 ~ PERF-14 | velocity | velocity anomaly · 50-turn window · progress stalled |
| PERF-15 ~ PERF-18 | 조합 | error+loop, backtrack+velocity, cache+backtrack, loop+cost |
| PERF-19 ~ PERF-22 | e2e 루프 | 실제 hook 통한 3rd-call loop-breaker |
| PERF-23 ~ PERF-25 | e2e 컨트롤 | 단일 호출, 2회 (boundary), 다른 파라미터 → no-loop |
| PERF-26 ~ PERF-30 | 추가 | 클린 ctx, velocity 단독, loop+error+cache 트리플, spurious trace, long+budget |

### Security (30)

| ID 범위 | 그룹 | 검증 내용 |
|---|---|---|
| SEC-01 ~ SEC-04 | filesystem | 재귀 purge × 3 (system / home / root) + force-push to main |
| SEC-05 ~ SEC-07 | k8s / helm | namespace / deployment removal · helm release teardown |
| SEC-08 ~ SEC-12 | cloud | terraform · EC2 terminate · IAM · gcloud · az |
| SEC-13 ~ SEC-14 | 컨테이너 | 권한 컨테이너 · 이미지 force-remove |
| SEC-15 ~ SEC-16 | SQL | drop-table · unbounded delete |
| SEC-17 ~ SEC-18 | 셸 | S3 bucket --force · 권한 install |
| SEC-19 ~ SEC-22 | 민감 경로 | AWS creds · SSH 키 · dotenv · passwd |
| SEC-23 ~ SEC-25 | 컨트롤 | Read /tmp · Bash echo · Grep TODO |
| SEC-26 ~ SEC-30 | unit | destructive_path_match · high blast (no destructive) · BLOCK no-domain · shadow read · security+cost combo |

---

## 환경 변수

`runner.setup_environment()` 가 자동으로 다음을 설정합니다 (이미 설정돼
있으면 보존):

| 변수 | 강제값 | 설명 |
|---|---|---|
| `AEGIS_LOCAL_AUDIT` | `/tmp/macmini-validation-audit.jsonl` | 매 실행마다 reset. |
| `AEGIS_ADVISOR_ENABLED` | `1` | advisor gate 켜짐. |
| `AEGIS_APPROVE_AS_BLOCK` | `1` | `REQUIRE_APPROVAL` 을 `BLOCK` 으로 격상 (테스트 결정성). |
| `AEGIS_ATMU_DISABLE` | `1` | sidecar ATMU 우회 (local 모드 전용). |
| `AEGIS_ADVISOR_PROVIDER` | `dummy` (setdefault) | 외부 키 없을 때 dummy. |
| `AEGIS_EMBEDDING_PROVIDER` | `dummy` (setdefault) | SHA3 기반 결정적 임베딩. |
| `AEGIS_JUDGE_PROVIDER` | `dummy` (setdefault) | 결정적 룰 기반 verdict. |
| `AEGIS_HW_PROVIDER` | `sim` (setdefault) | 시뮬레이터. |

> **사용자가 override 가능한 변수**: `AEGIS_*_PROVIDER` 만 `setdefault`
> 라서, 사전에 export 해두면 그 값이 우선합니다 (예: 실제 Haiku 로
> e2e 다시 검증하고 싶을 때 `AEGIS_JUDGE_PROVIDER=haiku` + 키).
> 결정성은 깨지므로 fail 이 늘어날 수 있음을 유의.

---

## 새 케이스 추가하기

### 1. unit 케이스 (heuristic 직접 호출)

`demo/macmini/cost.py` (또는 performance / security) 에 다음을 추가:

```python
add(TestCase(
    cid="COST-31",
    category="cost",
    title="my new boundary at 1.25x",
    scenario="...",
    execution_summary="cost_signals={'budget_used_ratio': 1.25}",
    test_type="unit",
    expected_advisor="cost-optimizer",
    expected_verb="swap-model",
    ctx_factory=ctx_5turns_expensive,
    current_model="claude-opus-4-7",
    cost_signals={"budget_used_ratio": 1.25},
))
```

각 파일 마지막의 `assert len(out) == 30` 도 같이 늘려 주세요.

### 2. e2e 케이스 (PreToolUse hook 통한 검증)

```python
add(TestCase(
    cid="SEC-31",
    category="security",
    title="my new destructive pattern",
    scenario="...",
    execution_summary="PreToolUse: Bash …",
    test_type="e2e",
    expected_advisor="security-reviewer",
    expected_verb="require-approval",
    pre_event={
        "hook_event_name": "PreToolUse",
        "session_id": "macmini-sec-31",
        "invocation_id": "macmini-sec-31-1",
        "tool_name": "Bash",
        "tool_input": {"command": cmd_my_destructive(...)},
    },
))
```

### 3. 민감한 명령어 리터럴 처리

step310 의 `DANGEROUS_PATTERNS` 가 *Write 페이로드 (즉 source 파일
자체)* 도 스캔합니다. 패턴은 다음 5종 (Python 정규식):

- 재귀 파일 purge — `\b` rm 토큰 + 옵션 + slash 시작 경로
- SQL 테이블 드롭 — drop + table 키워드
- 시스템 인증 파일 — `/etc/(shadow|passwd)`
- 권한 상승 — `\bsudo\s+`
- 직접 호출 — `\b(exec|system)\s*\(`

source 에 위 리터럴이 직접 들어가면 케이스 파일을 저장하는 행위 자체가
BLOCK 됩니다. 그래서 `fixtures.py` 는 토큰 분리 빌더로 우회합니다:

```python
def cmd_my_destructive(target: str) -> str:
    return _join("dangerous-tool", "delete", target)
```

소스 코드에서는 `cmd_my_destructive("prod")` 형태로만 참조하면, 빌드된
문자열은 런타임에만 존재합니다. 신규 빌더 추가 시 같은 패턴을
따라 주세요.

### 4. 기대값 (expectations) 옵션

| 필드 | 의미 |
|---|---|
| `expected_advisor` + `expected_verb` | 정확히 그 advisor 가 그 verb 를 emit |
| `expected_no_fire=True` | 어느 advisor 도 발화하면 안 됨 |
| `expected_no_fire_for="X"` | 특정 advisor 만 발화 금지 (다른건 허용) |
| `expected_multi=("A", "B", …)` | 여러 advisor 가 동시 발화 |
| `expected_verbs_any=("v1", "v2")` | 발화한 verb 합집합에 v1, v2 가 모두 포함 |
| `expected_decision="BLOCK"` | e2e 의 최종 decision 검증 |

---

## CI / 자동화 통합

### GitHub Actions

```yaml
- name: Mac mini 90-case validation
  run: |
    uv sync
    uv run python -m demo.macmini all --no-color --no-cases
```

Exit code 가 자동으로 비-제로일 때 step 이 fail 합니다. 리포트는
`docs/MACMINI_VALIDATION_REPORT.md` 에 자동 생성되므로
`actions/upload-artifact` 로 업로드 가능.

### pre-commit hook

```bash
# .git/hooks/pre-push
#!/usr/bin/env bash
exec uv run python -m demo.macmini all --no-color --no-cases
```

수정한 advisor 코드가 케이스를 깨뜨리면 push 가 거부됩니다.

### 회귀 추적

`docs/MACMINI_VALIDATION_REPORT.md` 는 결정적이라 매번 동일 출력. PR
diff 로 변화가 명시적으로 잡히도록 git 에 commit 해두기를 권장.

---

## 트러블슈팅

### `ImportError: aegis_local_hook` 또는 `post_tool`

`runner.setup_environment()` 가 `tools/` 와 `tools/hooks/` 를
`sys.path` 에 prepend 합니다. 그러나 `python -m` 이 아닌 직접 실행
(`python demo/macmini/__main__.py`) 으로 호출하면 패키지 경로가
바뀌어 import 가 실패합니다. **항상 `-m demo.macmini` 형태로** 호출.

### 케이스 일부가 갑자기 fail

원인 후보:

1. **Heuristic threshold 변경** — `src/aegis/judge/action_advice.py` 에서
   `budget_used_ratio >= 0.9`, `cache_hit_rate_max_drop_pp >= 30` 등이
   조정된 경우. 케이스 expectation 도 같이 고치세요.
2. **step311 regex 추가** — 새 destructive 패턴이 들어오면 기존 e2e
   ALLOW 컨트롤이 BLOCK 으로 바뀔 수 있습니다.
3. **loop detector 의 detection 메시지 포맷 변경** — `step336` 이
   emit 하는 trace 가 `"× seen"` (Unicode U+00D7) 을 포함해야 합니다.
   ASCII `x` 로 바뀌면 advisor 가 발화하지 않습니다.

### `BLOCK Write` — source 파일 자체가 BLOCK 됨

step310 가 Write 페이로드를 스캔하기 때문에, 새 케이스를 만들면서
destructive 패턴을 .py 파일에 직접 박으면 저장 자체가 막힙니다.
새 fixture 명령어 빌더는 `fixtures.py` 의 토큰-분리 패턴(`_join(...)`)
을 그대로 따라 주세요.

### `permission-escalator` 만 발화하고 `security-reviewer` 가 안 나옴

destructive 명령은 step311 의 정확한 regex 에 매치돼야 security-reviewer
경로로 들어갑니다. regex 가 매치하지 않으면 단순 BLOCK 으로 떨어져
permission-escalator 만 발화. step311 패턴 확인 후 fixture 의 명령어를
조정하세요. 예시: `az group delete` 는 step311 의 az 패턴
(`vm/sql/storage/keyvault`) 에 맞지 않아 단순 BLOCK 이지만,
`az vm delete` 는 매치돼 security-reviewer 발화.

### `MACMINI_VALIDATION_REPORT.md` 가 갱신 안 됨

`--report` 옵션을 다른 경로로 지정했는지 확인. 또는 마지막 실행이 중단됐는지
(예: `Ctrl+C`). 정상 종료까지 기다려 주세요.

### 캐시 / cumulative state

본 suite 는 매 실행마다:

- `/tmp/macmini-validation-audit.jsonl` 을 **삭제 후 재생성**
- `aegis.monitor.loop_detector` 의 default detector 를 `.reset()`
- `aegis_local_hook._CALIBRATION_SINGLETON = None` 으로 리셋

따라서 이전 실행이 다음 실행에 영향을 주지 않습니다. 만약 이상
거동이 보이면 `~/.aegis/` 디렉터리는 건드리지 않으니 그쪽 잔존
상태를 의심해 보세요.

---

## FAQ

**Q. Mac mini 외 환경 (M2 Pro, Linux x86, …) 에서도 돌아가나요?**
A. 네. 패키지가 stdlib 만 사용하고 GPU / CUDA / Docker / 네트워크 의존이
없으므로 macOS / Linux 어디서든 동일 결과가 나옵니다. "Mac mini" 라는
이름은 *최저 사양 보장 (저전력 ARM64 8GB)* 을 의도한 라벨입니다.

**Q. 90개를 다 돌리는데 얼마나 걸리나요?**
A. M2 Mac mini 기준 약 8~12초. CPython 시작 시간이 가장 큽니다. unit
케이스 65개는 합쳐서 50ms 미만, e2e 25개가 각 100~200ms 차지.

**Q. 실제 Anthropic Haiku 로 e2e 케이스를 다시 돌릴 수 있나요?**
A. `AEGIS_JUDGE_PROVIDER=haiku` + `ANTHROPIC_API_KEY=…` 로 export
후 다시 `python -m demo.macmini security` 를 실행하세요. 단,
heuristic 과 sLLM 은 *같은 schema 의 다른 brain* 이라 일부 verb 빈도가
달라질 수 있고, 더 많은 verb 가 emit 될 수 있습니다.
다른 결과를 보고 싶을 때 쓰는 모드입니다.

**Q. CI 에서 매번 90개 다 돌려야 하나요?**
A. 빠른 PR feedback loop 가 필요하면 `cost` 만 (`uv run python -m
demo.macmini cost`, ~3초). 머지 직전에는 `all` 권장.

**Q. ATMU / Sidecar / HW telemetry 도 검증되나요?**
A. 본 suite 의 범위 *밖*입니다. 본 suite 는 in-process advisor +
heuristic + step336 loop detector + step310/311 regex 만 다룹니다.
Sidecar M5/M9/M10/M14/M15/M16 검증은
`tests/integration/test_*sidecar*.py` 와 `demo/runtime_closed_loop.py` 가
담당합니다.

**Q. 케이스 ID 가 중간에 비면 안 되나요?**
A. 비어도 됩니다. `assert len(out) == 30` 만 맞으면 OK. 하지만 ID 는
*안정적인 참조점* 이므로 PR 에서 함부로 renumber 하지 마세요.

**Q. 실패 케이스를 일시적으로 skip 하고 싶어요.**
A. `cases()` 함수에서 해당 `add(TestCase(...))` 호출만 빼고 `assert
len(out)` 도 같이 줄이세요. 별도 skip 메커니즘은 없습니다 (skip 누적이
suite 의 신뢰를 갉아먹지 않도록 의도적으로 단순하게 유지).

**Q. 새 카테고리 (예: `compliance`) 를 추가하고 싶습니다.**
A. ① `demo/macmini/compliance.py` 에 `cases()` 함수 작성. ②
`demo/macmini/runner.py::_build_cases()` 에 분기 추가. ③
`demo/macmini/__main__.py::_VALID` tuple 에 추가. ④
`demo/macmini/render.py::render_summary` / `render_markdown` 의
`("cost", "performance", "security")` 튜플에도 추가.

---

## 부록 — 명령어 한 줄 요약

```bash
# ────── 일상 사용 ──────
uv run python -m demo.macmini all                  # 90개 전체
uv run python -m demo.macmini cost                 # Cost만
uv run python -m demo.macmini performance          # Performance만
uv run python -m demo.macmini security             # Security만
uv run python -m demo.macmini all --no-cases       # summary만
uv run python -m demo.macmini all --no-color       # CI / 로그용

# ────── 리포트 위치 변경 ──────
uv run python -m demo.macmini all --report /tmp/r.md

# ────── 결과 검증 (CI gate) ──────
uv run python -m demo.macmini all --no-cases || exit 1

# ────── 완전 재실행 (캐시 의심 시) ──────
unlink /tmp/macmini-validation-audit.jsonl 2>/dev/null
uv run python -m demo.macmini all

# ────── full pytest 같이 ──────
uv run pytest tests/ -q
uv run python -m demo.macmini all --no-cases
uv run ruff check demo/macmini/
uv run mypy demo/macmini/
```

---

**문서 버그 / 케이스 추가 제안**: GitHub issue 또는 PR 환영합니다.
이 매뉴얼은 PR #85 와 함께 1.0.0 릴리스 되었으며, suite 갱신 시
같이 업데이트됩니다.
