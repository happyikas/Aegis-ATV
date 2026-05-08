# 📊 ATV Live 사용자 매뉴얼

> **ATV Live** — Claude Code 위에서 동작하는 모든 agent 의 실행 현황을
> 실시간으로 모니터링. **Cost / Performance / Security** 3 축의 상태를
> 한 화면에 보여줍니다.
>
> 문서 버전: v0.1.0 · Aegis ATV personal MVP · 한국어 (정본)

---

## 1. ATV Live 가 보여주는 것

각 PreToolUse 호출은 firewall 16-step 파이프라인을 거치며 다음 정보를 남깁
니다:

```
Cost          → 토큰 / 달러 / 캐시 hit 비율 / 누적 예산 잔량
Performance   → 호출당 latency / 메모리 / 재시도 횟수 / loop count
Security      → verdict (ALLOW/REQUIRE_APPROVAL/BLOCK) + 매칭 룰 + 신뢰도
```

ATV Live 는 이 데이터를 **3가지 방식**으로 노출합니다:

| 방식 | 명령 | 용도 |
|------|------|------|
| 1. **Snapshot** (5줄 요약) | `aegis report` 또는 `/aegis-report` | "지금 상태 어때?" |
| 2. **Detail dashboard** | `aegis status --performance` | "왜 cache hit 이 떨어졌지?" |
| 3. **Daemon + 알림** | `aegis fleet-monitor start` | "임계값 넘으면 Slack/ntfy 으로" |

---

## 2. 빠른 시작 — 30초만에 현재 상태 확인

```bash
# 1) 5줄 요약 — 가장 자주 쓰게 되는 명령
aegis report --since 24h
```

출력 예시:

```
ATV Live — last 24h (session-id: claude-code-2026-05-08)
─────────────────────────────────────────────────────────
calls       : 312   (ALLOW 287 · APPROVE 18 · BLOCK 7)
cost        : $1.42  (cache hit 78%, ↓ from 82% yesterday)
performance : avg 217ms · p95 412ms · 0 loops detected
security    : 2 advisor signals — see `aegis advise --since 24h`
chain       : ✓ 312/312 records intact (sha3+ed25519)
```

이 5 줄은 그대로 **`/aegis-report` 슬래시 커맨드 출력** 과 같습니다 —
Claude Code 안에서 그냥 `/aegis-report` 만 치면 동일 출력이 나옵니다.

---

## 3. Cost — 토큰·달러·예산

### 3.1 즉시 요약

```bash
aegis cost summary --since 7d
```

출력:

```
ATV Live · cost (last 7d)
─────────────────────────────────────────────────
billed dollars        $9.41
input tokens          1,830,402   ($0.55 / Mtok cached, $3.00 / Mtok new)
output tokens           412,883   ($15.00 / Mtok)
cache hit rate         81.3%      ($4.21 saved by cache)
top-3 tools by cost:
  1. Read              $3.91   (412 calls, avg 14k input)
  2. Bash              $2.04   (812 calls, mostly small)
  3. Edit              $1.88   (203 calls, large diffs)
─────────────────────────────────────────────────
```

### 3.2 Per-tool / per-session 분석

```bash
# 지난 24h 동안 가장 비싼 도구 호출 5 개
aegis cost summary --since 24h --top 5

# 특정 세션만
aegis cost summary --session claude-code-2026-05-08

# 여러 agent 병렬 실행 시 cross-agent rollup
aegis cost multi-agent --since 24h
```

### 3.3 예산 (`step335`) 게이트

```bash
# 세션당 토큰 예산 설정 (예: 50K input + output 합산)
aegis budget set --session-token-cap 50000

# 현재 예산 상태
aegis budget show
```

예산 게이트가 켜져 있으면, 임계값을 넘는 시점부터 모든 PreToolUse 가
REQUIRE_APPROVAL 로 전환됩니다 — Claude Code 의 무한 retry 가 비싼 청구서
를 만드는 케이스를 막습니다.

---

## 4. Performance — Latency·Cache·Loop

### 4.1 Performance 대시보드

```bash
aegis status --performance --since 24h
```

출력:

```
ATV Live · performance (last 24h)
──────────────────────────────────────
calls          : 312
firewall       : avg 4.2ms  p95 12ms  p99 38ms
sLLM judge     : avg 47ms   p95 102ms  (haiku, 18 calls only)
case memory    : avg 1.1ms  p95 3.4ms  (skipped sLLM 84 times)
cache          : 78% hit rate (saved est. $4.21)
loops          : 0 detected (step336 threshold = 3)
inefficiency   : 0 backtrack · 2 redundant · 1 retry
──────────────────────────────────────
```

### 4.2 Loop detector 가 잡은 것 보기

```bash
# 같은 호출이 3+ 회 반복된 케이스 검색
aegis session show --loop-detected --since 7d
```

각 케이스는 `~/.aegis/audit.jsonl` 에 정확한 trace 가 남아있어, 필요하면
[🔧 Doctor](DOCTOR_MANUAL.ko.md) 의 `aegis forensic <trace>` 로 깊이 들여
다볼 수 있습니다.

---

## 5. Security — Verdict 분포·Drift·Advisor signal

### 5.1 Security 요약

```bash
aegis report --since 7d --kind security
```

출력:

```
ATV Live · security (last 7d)
──────────────────────────────────────
verdicts:
  ALLOW              2,047   (94.1%)
  REQUIRE_APPROVAL      88   (4.0%)
  BLOCK                 41   (1.9%)

top BLOCK reasons:
  1. step311 cloud destructive (kubectl delete)    11
  2. step310 path traversal                          8
  3. step309 instruction drift                       7
  4. step336 loop detector                           7
  5. step340 case-memory match                       8

baseline drift: ✓ no drift detected (last reattest 2 days ago)
chain integrity: ✓ 2,176/2,176 records intact
──────────────────────────────────────
```

### 5.2 Drift 모니터링 (step309)

`CLAUDE.md`, `AGENTS.md`, `.mcp.json`, plugin/skill manifest 가 변경되면
자동으로 BLOCK 됩니다 — 변경이 의도된 것이라면 baseline 을 갱신:

```bash
# 현재 instruction 파일 상태를 새 baseline 으로 attest
aegis baseline reattest

# baseline 경로
export AEGIS_INSTRUCTION_BASELINE_PATH=~/.aegis/instruction_baseline.json
```

---

## 6. Daemon 모드 — Fleet Monitor + 알림

여러 agent / 여러 세션 / 여러 프로젝트를 하나로 모니터링하고, 임계값을
넘으면 Slack 이나 ntfy.sh 로 알림을 받고 싶다면:

```bash
# daemon 시작
aegis fleet-monitor start \
    --notify slack \
    --slack-webhook https://hooks.slack.com/... \
    --threshold-cost 5.00 \
    --threshold-block-rate 0.10

# 상태 확인
aegis fleet-monitor status

# 종료
aegis fleet-monitor stop
```

알림 트리거:

- 시간당 비용 > `--threshold-cost`
- 시간당 BLOCK 율 > `--threshold-block-rate`
- chain integrity 실패 (즉시)
- baseline drift 감지 (즉시)

---

## 7. ATMU — Agent Telemetry Management Unit (M10)

각 도구 호출은 7-state 트랜잭션으로 추적됩니다:

```
tentative → prepared → committed
                    ↘ aborted
                    ↘ rolled-back
                    ↘ compensated
                    ↘ quarantined
```

평소엔 신경쓰지 않아도 됩니다 — `committed` 가 정상 종료, 나머지는 모두
이상 신호입니다. 이상 신호 카운트는 ATV Live 의 security 요약에 묶여 출력됩
니다.

상세 상태가 필요하면:

```bash
aegis session list --kind tx
aegis session show <session_id> --tx-states
```

---

## 8. 슬래시 커맨드 (Claude Code 안에서)

| 슬래시 | 동등한 CLI | 용도 |
|--------|-----------|------|
| `/aegis-report` | `aegis report --since 24h` | 5 줄 요약 |
| `/aegis-verify` | `aegis verify-audit` | chain 무결성 검증 |
| `/aegis-help` | — | 슬래시 메뉴 |

`/aegis-advise` 와 `/aegis-forensic` 은 Doctor 영역입니다 — [🔧
DOCTOR_MANUAL.ko.md](DOCTOR_MANUAL.ko.md) 참조.

---

## 9. 환경 변수 레퍼런스

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `AEGIS_TOKEN_BUDGET` | per-session 토큰 예산 (step335) | unset |
| `AEGIS_LOOP_THRESHOLD` | loop 감지 임계값 (step336) | `3` |
| `AEGIS_INSTRUCTION_BASELINE_PATH` | step309 baseline 파일 경로 | unset |
| `AEGIS_FLEET_MONITOR_INTERVAL` | daemon polling 주기 (초) | `60` |
| `AEGIS_NOTIFY_SLACK_WEBHOOK` | fleet-monitor 슬랙 webhook | unset |
| `AEGIS_NOTIFY_NTFY_TOPIC` | fleet-monitor ntfy.sh topic | unset |

---

## 10. 자주 묻는 질문

**Q. cache hit rate 가 갑자기 50% 로 떨어졌어요. 왜?**
A. `aegis cost summary --since 24h --kind cache` 로 시간별 hit rate 추이를
확인. 큰 prompt 가 매번 새로 만들어지면 cache miss → Claude Code 의
session 분기, 새 system prompt, 또는 `--no-cache` 옵션 의심.

**Q. BLOCK 율이 갑자기 올라갔어요. 어떻게 원인 찾나?**
A. `aegis report --since 1h --kind security` 로 top reason 확인 → 의심
trace 를 [🔧 Doctor](DOCTOR_MANUAL.ko.md) 의 `aegis forensic <trace>` 로
깊이.

**Q. fleet-monitor daemon 이 죽으면 어떻게 되나?**
A. firewall 자체는 daemon 과 무관합니다 — daemon 은 알림만 보냅니다. daemon
이 죽어도 `aegis report` 는 그대로 audit log 를 읽어서 출력합니다.

**Q. cost 데이터가 외부로 나가나?**
A. 0 — Solo Free 컨트랙트. cost 는 `~/.aegis/audit.jsonl` 의 token usage
필드를 합산할 뿐. fleet-monitor 의 Slack/ntfy 알림은 명시적으로 켰을
때만 외부 호출.

---

## 11. 관련 문서

- [🏋️ COACH_MANUAL.ko.md](COACH_MANUAL.ko.md) — 학습 결과로 더 정확한 모니
- [🔧 DOCTOR_MANUAL.ko.md](DOCTOR_MANUAL.ko.md) — Live 가 잡은 이상치를
  심층 분석·해결
- [PERSONAL_QUICKSTART.md](../PERSONAL_QUICKSTART.md) — 5분 설치
- [OPERATIONS.md](../OPERATIONS.md) — sidecar 모드 운영자용 prod runbook
