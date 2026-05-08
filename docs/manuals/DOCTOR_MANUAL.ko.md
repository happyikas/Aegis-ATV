# 🔧 ATV Doctor 사용자 매뉴얼

> **ATV Doctor** — agent 가 문제를 일으켰거나, 일으킬 가능성이 높아질 때
> log 를 분석해서 **fix** 하거나 **advice** 를 제공하는 기능.
>
> 문서 버전: v0.1.0 · Aegis ATV personal MVP · 한국어 (정본)

---

## 1. ATV Doctor 가 하는 일

[📊 Live](LIVE_MANUAL.ko.md) 가 *지금 무슨 일이 벌어지고 있나* 를 보여준다
면, Doctor 는 **이미 일어난 / 일어나려는 문제** 를 다음 두 방향에서 다룹
니다:

| 방향 | 도구 | 입력 | 출력 |
|------|------|------|------|
| **사후 분석 (Forensic)** | `aegis forensic` | session id 또는 trace id | 한 호출의 전체 step trace + 매칭 룰 + advisor signal |
| **실시간 추천 (Advise)** | `aegis advise` | 최근 N 호출 audit log | 8-advisor pipeline 의 cost / perf / security / compliance 추천 |
| **롤백 (Rollback)** | `aegis rollback` | invocation_id / session / --since 시간 | 해당 호출이 만든 파일 변경 / 명령 실행을 되돌림 |
| **헬스 체크 (Health)** | `aegis health` | (없음) | malfunction signal — daemon 죽음, key 만료, log 파일 손상 등 |

---

## 2. 빠른 시작 — 사고 났을 때 30초 트리아주

```bash
# 1) 마지막 세션의 forensic timeline (가장 자주 쓰는 명령)
aegis forensic last

# 2) Advisor 추천 — "지금 무슨 조치를 해야 하나"
aegis advise --since 24h

# 3) 모든 audit chain 무결성 확인
aegis verify-audit

# 4) Aegis 자체 헬스
aegis health
```

이 4 개 명령으로 **"방금 무슨 일이 일어났는지" + "지금 뭘 해야 하는지" +
"증거가 위변조되지 않았는지" + "도구 자체에 문제 없는지"** 를 30 초 안에
확인할 수 있습니다.

---

## 3. Forensic — 한 호출의 전체 trace 보기

`aegis forensic <selector>` 는 selector 가 가리키는 호출(들) 의 chronologi
cal timeline 을 출력합니다.

### 3.1 Selector 종류

```bash
aegis forensic last                    # 가장 최근 호출 1 개
aegis forensic claude-code-2026-05-08  # session_id 전체 (시간순)
aegis forensic 40312fd2                # trace_id (8자 prefix)
aegis forensic --since 1h              # 지난 1 시간 모든 호출
aegis forensic --verdict BLOCK         # BLOCK 만 필터
```

### 3.2 출력 해석

```
Forensic timeline · trace=40312fd2 · 2026-05-08T15:32:11Z
─────────────────────────────────────────────────────────
tool         Bash
input        <재귀 디렉터리 삭제 명령 — system path 대상>
session      claude-code-2026-05-08
verdict      BLOCK   confidence=0.97   latency=4.2ms

step 305 safe_actions     pass
step 309 instruction      pass (baseline ok)
step 310 patterns         MATCH  pattern=recursive_destructive
step 311 cloud_destruct   pass
step 312 normalize        ok
step 320 atv_vector       computed (30 fields)
step 330 retrieval        topK=3 (similar BLOCKs)
step 335 cost_gate        pass (under budget)
step 336 loop_detector    pass
step 340 case_memory      MATCH (cosine=0.94 → BLOCK case #2031)
step 345 sLLM judge       skipped (case-memory short-circuit)

advisor signals:
  security    [HIGH]  Recursive removal of system path under /var
  reliability [MED]   Operation has no rollback
  compliance  [MED]   No prior `git status` in session

audit chain  ✓ this_hash=8a91f2..  prev_hash=ee9dbc..
signature    ✓ ed25519 verified (key fp f2a17931406e4f56)
```

각 step 결과를 위에서 아래로 읽으면 **왜 이 verdict 가 나왔는지** 가 그대
로 보입니다. step 310 (regex pattern match) 또는 step 340 (case memory) 에
서 결정 난 경우가 가장 많습니다.

### 3.3 슬래시 커맨드

Claude Code 안에서:

```
/aegis-forensic last       — 마지막 호출
/aegis-forensic <trace>    — 특정 trace
```

---

## 4. Advise — Advisor pipeline 추천

`aegis advise` 는 8 개 advisor 의 추천을 합산해서 출력합니다.

### 4.1 8-advisor 종류

| Advisor | 무엇을 추천 |
|---------|-------------|
| **cost** | 토큰 / 캐시 효율 개선, 예산 초과 위험 |
| **latency** | 느린 도구 호출, 직렬 의존 단순화 |
| **reliability** | 재시도 패턴, 실패 후 보상 트랜잭션 |
| **security** | 위험한 명령, 권한 승격 prefix 사용, 파일 무결성 |
| **compliance** | 감사 로그 누락, baseline drift, 키 만료 |
| **safety** | 시스템 경로, 멀티 사용자 영향 |
| **efficiency** | redundant 호출, dead-end 패턴, backtrack |
| **governance** | 정책 변경 미반영, 승인 프로세스 우회 |

### 4.2 Profile 별 차이

`aegis advise` 의 출력은 `--profile` 에 따라 깊이가 다릅니다:

| Profile | Advisor 활성화 | 비용 |
|---------|----------------|------|
| `free` (기본) | dummy advisor — 단순 룰 기반, 외부 호출 0 | $0 |
| `pro` | 로컬 sLLM (Phi-3.5-mini, GGUF) — 진짜 자연어 추천 | $0 (로컬) |
| `cloud` | Anthropic Haiku 기반 8-advisor 풀 파이프라인 | ~$0.001/추천 |

```bash
# 한 번에 가장 좋은 추천을 받고 싶으면 cloud
aegis advise --since 24h --profile cloud
```

### 4.3 출력 예시 (cloud profile)

```
ATV Doctor · advise (last 24h, profile=cloud)
─────────────────────────────────────────────
[HIGH] cost — 4 tool calls hit cache miss in same session
   suggestion: 같은 prompt prefix 가 반복되는데 cache hit 이 0%.
   `--prompt-cache` 활성화 또는 prompt 첫 1.5 KB 를 system message 로
   이동하면 추정 $0.42 절감. trace=2a7b91e3, 4d1e22f0, 5b88ea1c

[MED] security — destructive op in repeat pattern (5 calls)
   suggestion: 5 분 안에 같은 namespace 에 5 회 destructive op.
   step336 loop detector 가 잡았지만 step340 case memory 에서
   BLOCK 케이스가 없어 통과. baseline reattest 권장.
   trace=11e9f2c8

[LOW] efficiency — redundant Read(same_file)×3
   trace=0aa2b1d5
─────────────────────────────────────────────
3 recommendations · use `aegis forensic <trace>` for any of them
```

`aegis advise` 출력에 traceID 가 함께 나오기 때문에 그대로 `aegis forensic
<trace>` 로 깊이 들어갈 수 있습니다.

### 4.4 슬래시 커맨드

Claude Code 안에서:

```
/aegis-advise              — 지난 24h 추천
```

---

## 5. Rollback — 사고 친 호출 되돌리기

agent 가 의도와 다르게 파일을 지웠거나 잘못된 명령을 실행했을 때:

### 5.1 한 호출만 롤백

```bash
# trace_id 로 정확히 한 호출
aegis rollback 40312fd2

# 출력:
#   ATV Doctor · rollback trace=40312fd2
#   원본 op: Bash(<destructive op>)
#   restoration: 3 files restored from snapshot at 2026-05-08T15:31:09Z
#   ✓ done
```

### 5.2 세션 전체 롤백

```bash
aegis rollback --session claude-code-2026-05-08

# 출력:
#   ATV Doctor · rollback session=claude-code-2026-05-08
#   17 invocations to revert (chronologically reverse)
#   ─ proceed? [y/N]
```

### 5.3 시간 기반 롤백

```bash
# 지난 30 분 안에 일어난 모든 destructive op 되돌리기
aegis rollback --since 30m --filter destructive
```

### 5.4 어떤 호출이 롤백 가능한가?

`aegis forensic` 출력에 `rollback: snapshot present (id=...)` 가 있으면
가능합니다. snapshot 은 ATMU (M10) 의 prepared phase 에서 자동 생성되며,
파일 기준 destructive op (재귀 삭제, 이동, truncate 등) 와 일부 SQL 명령
에 대해 보존됩니다.

스냅샷 정책:

```bash
# 보유 기간
aegis snapshots list

# 14 일 이전 스냅샷 정리
aegis snapshots prune --older-than 14d
```

---

## 6. Health — Aegis 자체의 이상 신호

```bash
aegis health
```

출력:

```
Aegis health check
──────────────────────────────────────
firewall hook         ✓ registered (~/.claude/settings.json)
slash commands        ✓ 5 installed (~/.claude/commands)
audit log             ✓ 14,728 records (~/.aegis/audit.jsonl)
chain integrity       ✓ verified (sha3-256)
ed25519 signing       ✓ key fp f2a17931406e4f56
sLLM judge            ✓ dummy (free profile)
fleet-monitor         ─ not running
baseline drift        ✓ no drift (last reattest 2 days ago)
─ no malfunction signals detected
```

문제가 있으면 `[!]` 로 표시되며, 권장 조치가 함께 나옵니다:

```
audit log             [!] 17 records have invalid signature
                          → run `aegis verify-audit --explain` for details
                          → if mutation suspected, see DOCTOR_MANUAL §7
```

---

## 7. 사고 시나리오 별 플레이북

### 7.1 "agent 가 파일을 지웠는데 어떤 파일을 지웠는지 모르겠다"

```bash
# 1) 의심 시간대의 destructive op 찾기
aegis report --since 1h --kind destructive

# 2) 후보 trace 식별 후 forensic
aegis forensic <trace>

# 3) snapshot 있으면 롤백
aegis rollback <trace>
```

### 7.2 "비용이 갑자기 폭증했다"

```bash
# 1) 비용 집중 구간 찾기
aegis cost summary --since 24h --top 10

# 2) 가장 비싼 세션 forensic
aegis forensic <session_id>

# 3) Advisor 추천
aegis advise --since 24h --profile cloud
```

### 7.3 "감사관이 audit log 를 검증해달라고 한다"

```bash
# 1) Chain 무결성 (default — sha3 hash chain)
aegis verify-audit

# 2) 서명까지 확인 (Ed25519, opt-in)
aegis audit-key show
aegis verify-audit --strict

# 3) 외부 검증용 public key 공유
cat ~/.aegis/keys/audit.ed25519.pub
```

### 7.4 "agent 가 같은 호출을 무한 반복한다"

```bash
# 즉시 차단
export AEGIS_LOOP_THRESHOLD=2     # 더 엄격하게

# 그 동안 일어난 loop 확인
aegis session show --loop-detected --since 1h

# 패턴이 반복되면 case-memory 에 BLOCK 케이스로 학습 추가
# (Coach 매뉴얼 §4.2 참조)
```

### 7.5 "CLAUDE.md 가 의도치 않게 수정됐다"

```bash
# 1) 현재 baseline 과의 diff
aegis baseline diff

# 2) 의도된 변경이면 reattest
aegis baseline reattest

# 3) 의도되지 않은 변경이면 git 으로 복원 후 reattest
git checkout HEAD -- CLAUDE.md
aegis baseline reattest
```

---

## 8. 환경 변수 레퍼런스

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `AEGIS_JUDGE_PROVIDER` | sLLM judge (`dummy`/`local`/`haiku`) | `dummy` |
| `AEGIS_JUDGE_MODEL_PATH` | 로컬 sLLM 모델 경로 (GGUF) | unset |
| `AEGIS_ADVISOR_ENABLED` | advisor pipeline 활성화 | `1` |
| `AEGIS_ROLLBACK_SNAPSHOT_DIR` | 스냅샷 저장 경로 | `~/.aegis/snapshots` |
| `AEGIS_INSTRUCTION_BASELINE_PATH` | step309 baseline 파일 경로 | unset |

---

## 9. 자주 묻는 질문

**Q. forensic 결과를 외부 (예: SOC) 로 export 할 수 있나?**
A. `aegis forensic <trace> --format json` → JSON 으로 출력. audit log 자체
가 JSONL 이라 `~/.aegis/audit.jsonl` 을 그대로 send 해도 됩니다.
검증자는 `aegis verify-audit --file received.jsonl --pubkey
peer.pub` 으로 검증.

**Q. advise 의 추천이 항상 정확한가?**
A. `free` profile 의 dummy advisor 는 룰 기반이라 false positive 가 있을
수 있습니다. `pro` (로컬 sLLM) 또는 `cloud` (Haiku) profile 에서는 자연어
추천 + 신뢰도 점수가 함께 나옵니다. 추천이 부정확하면 [🏋️ Coach](
COACH_MANUAL.ko.md) §5 의 advisor calibration 으로 가중치 보정.

**Q. rollback 으로 모든 것이 원상복구 되나?**
A. 파일 시스템 op 와 일부 SQL 은 가능, 외부 API 호출 (네트워크 send,
cloud delete 등) 은 불가능합니다. ATMU 가 어떤 호출을 보상 가능 / 불가
능으로 분류했는지는 `aegis forensic <trace> --rollback-info` 출력에
나옵니다.

**Q. 외부 호출이 발생하나?**
A. `--profile free` (기본) 에서는 0. `--profile cloud` 에서 advisor pipe
line 만 Anthropic API 로 호출. forensic / rollback / health 는 모두
fully local.

---

## 10. 관련 문서

- [📊 LIVE_MANUAL.ko.md](LIVE_MANUAL.ko.md) — 이상 신호를 가장 먼저 감지
- [🏋️ COACH_MANUAL.ko.md](COACH_MANUAL.ko.md) — Doctor 의 추천 정확도를
  학습으로 개선
- [PERSONAL_QUICKSTART.md](../PERSONAL_QUICKSTART.md) — 5분 설치
- [DOGFOOD.md](../DOGFOOD.md) — 실제 incident replay 예시
