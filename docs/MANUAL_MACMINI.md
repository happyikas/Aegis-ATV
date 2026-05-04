# Aegis MVP — Mac mini 운영 매뉴얼 (Plugin Solo Free)

**대상**: Mac mini (M-시리즈) 단일 개발자 환경에서 Claude Code 보안 hook을 in-process로 가동하고, 며칠간 자기 트래픽으로 M13 attribution head를 본인 패턴에 calibrate하는 흐름.

**전제 버전**: `main` HEAD ≥ `ce03e99` (PR #31 ATMU plugin 통합 + PR #32 HW telemetry 와이어링 fix 반영). 이 두 PR이 빠지면 일부 단계가 동작하지 않습니다.

**관련 문서**:
- [`SETUP_MACMINI.md`](../SETUP_MACMINI.md) — Sidecar (Docker) 모드 부트스트랩
- [`docs/QUICKSTART.md`](QUICKSTART.md) — 60초 sidecar 데모
- [`docs/MANUAL_v2.2.md`](MANUAL_v2.2.md) — 풀 사용자 매뉴얼 (sidecar 기준)
- [`docs/RUNBOOK.md`](RUNBOOK.md) — 운영 룬북

---

## 0. 개요 — 한 페이지 흐름

```
┌─ 1. 설치 (5분) ─────────────────────────┐
│  Homebrew + uv → git clone → uv sync     │
│  → cp .env.example .env                  │
│  → uv run aegis install --mode local     │
│    --judge dummy                         │
└──────────────────────────────────────────┘
                  │
┌─ 2. 데이터 수집 활성화 (1분) ────────────┐
│  ~/.claude/settings.json 1줄 sed:        │
│    AEGIS_HW_PROVIDER=sim                 │
│    AEGIS_BURNIN_SHADOW=1                 │
│    AEGIS_APPROVE_AS_BLOCK=0              │
│  → Claude Code 재시작                     │
└──────────────────────────────────────────┘
                  │
┌─ 3. 검증 (3분) ─────────────────────────┐
│  pytest / dogfood_check / scenarios      │
└──────────────────────────────────────────┘
                  │
┌─ 4. 일상 사용 (며칠) ───────────────────┐
│  자연스럽게 Claude Code 사용             │
│  매 hook 호출 → ATV 기록 → shadow 누적    │
│  하루 1~2회 진행 확인                    │
└──────────────────────────────────────────┘
                  │
┌─ 5. 재학습 사이클 (반복) ────────────────┐
│  shadow-status → train-m13 →             │
│    compare-m13 → v2 채택 or 반려         │
└──────────────────────────────────────────┘
```

---

## 1. 사전 요구사항

```bash
# Homebrew (없으면)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# uv (Python 패키지 매니저)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

- Claude Code 데스크톱 앱이 이미 설치되어 있어야 합니다 (`open -a 'Claude Code'`로 동작 확인).
- Python 3.11+ 는 `uv sync`가 자동으로 보장합니다.
- API 키 / Docker / OrbStack 모두 **불필요** (Solo Free 컨트랙트).

---

## 2. 설치

```bash
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
git log --oneline -1
# 최소: ce03e99 plugin-mode HW telemetry: feed simulate_from_env into build_atv (#32)

uv sync                          # 60 packages, ~30s
cp .env.example .env             # dummy provider 기본값 그대로 두면 됨
```

코드 무결성 1차 검증:

```bash
uv run pytest -q                 # 1443 passed, 13 skipped
uv run ruff check .              # All checks passed!
uv run mypy src                  # 136 source files clean
```

세 가지 다 그린이어야 다음 단계로 진행.

---

## 3. Claude Code에 hook 설치

```bash
uv run aegis install --mode local --judge dummy
```

- `--judge dummy` : 데이터 수집 단계에 권장. 키워드 기반(`transfer/delete/shutdown/drop` + 고위험 tool) 즉시 판정. 외부 의존 0개. **사용자 흐름을 거의 막지 않음** (오직 명백한 위험 키워드만 BLOCK).
- 끝나고 hybrid로 승격할 때만 `--judge hybrid`로 재실행. 그러나 v1 weights는 합성 데이터로 학습된 것이라 실제 트래픽에서 false-positive가 많음 → **본인 데이터로 v2를 학습한 뒤** 켜는 것이 권장 흐름.

설치 결과 확인:

```bash
cat ~/.claude/settings.json | python3 -m json.tool
# hooks.PreToolUse / PostToolUse / Stop 3개 모두 등록되어 있어야 함
```

---

## 4. 데이터 수집 + HW sim 활성화

`aegis install`이 만든 hook 명령어에 다음 3개 env를 prepend:

| Env | 의미 |
|---|---|
| `AEGIS_HW_PROVIDER=sim` | HW band 200-D를 시뮬레이터가 채움 → step337 + M12 cost-divergence 활성 |
| `AEGIS_BURNIN_SHADOW=1` | 매 PreToolUse마다 (ATV, verdict) 페어를 `~/.aegis/shadow.jsonl`에 누적 → M13 v2 학습 corpus |
| `AEGIS_APPROVE_AS_BLOCK=0` | REQUIRE_APPROVAL이 BLOCK으로 격상되지 않고 경고만 출력 → 수집 단계에 사용자 흐름 막지 않음 |

**1줄 sed** (Pre + Post hook 양쪽 한 번에 처리):

```bash
sed -i '' \
  -e 's|AEGIS_EMBEDDING_PROVIDER=|AEGIS_HW_PROVIDER=sim AEGIS_BURNIN_SHADOW=1 AEGIS_APPROVE_AS_BLOCK=0 AEGIS_EMBEDDING_PROVIDER=|' \
  -e 's|PYTHONPATH=\([^ ]*\) /|AEGIS_HW_PROVIDER=sim AEGIS_BURNIN_SHADOW=1 PYTHONPATH=\1 /|' \
  ~/.claude/settings.json
```

(첫 패턴은 PreToolUse 명령어, 두 번째는 PostToolUse 명령어와 매치. PostToolUse는 이미 첫 매치에서 처리되지 않은 경우만 두 번째에서 처리됨.)

검증:

```bash
grep -E "AEGIS_(HW_PROVIDER|BURNIN_SHADOW|APPROVE_AS_BLOCK)" ~/.claude/settings.json
# 두 줄 모두 세 env 보여야 함
```

**Claude Code 데스크톱 앱 재시작** — settings.json은 새 세션에서만 다시 로드됩니다.

---

## 5. 셋업 검증

### 5.1 Dogfood 사전 체크 (8개)

```bash
./scripts/dogfood_check.sh
```

**Exit 0 + "11/11 checks passed"** = 진짜 Claude Code에 트래픽 흘려도 안전.

5개 skip은 모두 `llama-cpp-python` 미설치(선택 사항)에 따른 정상 skip.

### 5.2 7-Scenario 회귀 (incident response)

```bash
./scripts/macmini_user_test.sh
```

dummy judge로는 **5/7 pass**가 정상:
- ✅ 1 (DB destruction), 3 (instruction drift), 5 (cost runaway), 6 (capability escalation), 7 (sensitive path)
- ❌ 2 (AWS secret) — 의미적 판정 필요, hybrid+v2 학습 후 통과
- ❌ 4 (tool loop) — 시간 패턴 필요, hybrid 또는 step336이 실시간 누적된 후 통과

리포트는 `./reports/<timestamp>/` 에 저장.

### 5.3 ATMU 2PC + HW sim 동작 직접 확인

Claude Code 세션에서 임의의 tool 호출 (예: `ls -la`) 후:

```bash
uv run aegis status
```

기대 출력:

```
audit chain:    N records   OK
PreToolUse:     N   ALLOW X  BLOCK Y  ASK Z
PostToolUse:    M   ok ... fail ... timeout ...
ATMU intents:   N   committed X  aborted Y  prepared Z  tentative W
sLLM daemon:    stopped
```

`PostToolUse` 카운트가 0이 아니어야 함 → ATMU 2PC가 plugin mode에서 닫히고 있다는 증거.

직전 audit record의 `step337` 트레이스 확인:

```bash
tail -1 ~/.aegis/audit.jsonl | python3 -c "
import sys, json
r = json.loads(sys.stdin.read())
t = r['explain']['step_traces'].get('aegis.firewall.step337_hw_anomaly.run','?')
print(t)"
```

기대: `step337: HW signals nominal` (또는 anomaly 발견 시 BLOCK rule).
**금지**: `step337: HW band zero (T2 default)` — 이 메시지가 나오면 PR #32가 안 들어왔거나 `AEGIS_HW_PROVIDER=sim` env가 hook에 안 박혀있다는 뜻.

---

## 6. 일상 운영 — 모니터링 명령 4종

```bash
# (a) Plugin 전체 상태 — 매일 1회
uv run aegis status

# (b) Shadow corpus 진행도 — 30 → 200 → 1000 누적 추세 관찰
uv run aegis burnin shadow-status

# (c) 감사 체인 무결성 — 변조 시도 감지
uv run aegis verify-audit

# (d) 직전 결정 5줄 요약 — daily report
uv run aegis report
```

**필요 시**:

```bash
# 특정 결정의 layer-by-layer "왜" 추적 (해당 trace_id가 BLOCK이면 어디서 막혔는지)
uv run aegis report --explain <trace_id_first_8_chars>

# 감사 로그 회전 + 무결성 일괄 검증
uv run aegis audit verify
```

---

## 7. 재학습 사이클 — v1 → v2

### 7.1 학습 시점 결정

`aegis burnin shadow-status` 출력의 다음 두 가지를 확인:

1. **레코드 수** ≥ 30 (필수, train-m13의 하드 임계). 200+ 권장.
2. **라벨 분포 균형** — ALLOW 60% 이하가 이상적. ALLOW 90% 같으면 v2가 "다 ALLOW"로 학습되어 v1보다 후퇴함.

균형이 안 맞으면 의도적으로 공격 사례를 주입해 누적:

```bash
./scripts/macmini_user_test.sh                # 7 incident → 14개 레코드 누적
./scripts/macmini_user_test.sh --hybrid       # hybrid 모드로도 한번 (현재 dummy면 임시 전환 후 복귀)
```

### 7.2 학습 + 비교

```bash
uv run aegis burnin train-m13 --corpus ~/.aegis/shadow.jsonl
# → models/m13_attribution_head_v2.json 생성

uv run aegis burnin compare-m13
# → v1 vs v2 매트릭스 출력
```

판정 기준 (`compare-m13`이 자동 계산):

| Metric | 의미 | 좋은 방향 |
|---|---|---|
| 3-class accuracy | ALLOW/BLOCK/ASK 정확도 | ↑ |
| False negatives (FN) | 위험을 놓침 | ↓ (5×가중) |
| False positives (FP) | 정상을 막음 | ↓ |
| Asym cost | `5×FN + FP` | ↓ |
| Winner | v1 or v2 | v2 채택 가능 시 표시 |

### 7.3 채택 / 반려

**v2가 이기면**:

```bash
mv models/m13_attribution_head_v2.json models/m13_attribution_head_v1.json
./scripts/macmini_user_test.sh --hybrid       # AWS secret + loop 시나리오 통과 확인
```

만족스러우면 hybrid로 복귀:

```bash
sed -i '' 's/AEGIS_JUDGE_PROVIDER=dummy/AEGIS_JUDGE_PROVIDER=hybrid/' ~/.claude/settings.json
# Claude Code 재시작
```

**v2가 지면** (예: 첫 시도에서 자주 발생 — 데이터 불균형):

- v2 파일 삭제: `rm models/m13_attribution_head_v2.json`
- 며칠 더 데이터 수집 + 균형 보강 후 7.2 반복.

---

## 8. 트러블슈팅

### 8.1 Hook이 본인 작업을 막을 때 (hybrid의 false-positive)

증상: `PreToolUse:Bash hook error: BLOCK ... attribution-head BLOCK (score=N ≥ 0.70)`.

원인: hybrid의 v1 가중치가 합성 데이터 기반이라 실제 복잡한 Bash/Edit/Write에 과민.

해결:

```bash
# 외부 터미널 (iTerm 등)에서 — Claude Code 안에서 실행하면 본인이 본인을 막음
sed -i '' 's/AEGIS_JUDGE_PROVIDER=hybrid/AEGIS_JUDGE_PROVIDER=dummy/' ~/.claude/settings.json
# Claude Code 재시작 → dummy 모드, 데이터 수집 계속
```

v2 학습이 만족스러워지면 다시 hybrid로 승격.

### 8.2 Hook 자체를 잠시 끄고 싶을 때

```bash
uv run aegis uninstall                # ~/.claude/settings.json 백업 → 복원
# 또는 수동으로 settings.json에서 hooks 키만 삭제
```

복구:

```bash
uv run aegis install --mode local --judge dummy
# + 4장의 sed로 env 주입
```

### 8.3 `aegis status` 결과의 PreToolUse / PostToolUse 카운트가 다름

자연스러움. 다음 두 경우에 한쪽만 기록됨:

1. PreToolUse가 BLOCK → tool 실행 안 됨 → PostToolUse 안 옴
2. 매우 빠른 tool 실행 시 PostToolUse가 누락되는 Claude Code 버그 (드뭄)

ATMU intent 수 = PreToolUse 수가 정상 (단, ATMU는 PR #31 머지 이후의 호출만 추적).

### 8.4 Shadow 레코드가 안 늘어남

체크리스트:

```bash
# (a) env가 hook에 박혀있나
grep AEGIS_BURNIN_SHADOW ~/.claude/settings.json

# (b) Claude Code가 새 settings.json을 읽었나 — 앱 재시작 필요

# (c) 직접 호출 테스트
echo '{"hook_event_name":"PreToolUse","session_id":"t","invocation_id":"x","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | AEGIS_BURNIN_SHADOW=1 uv run python tools/aegis_local_hook.py
ls -l ~/.aegis/shadow.jsonl
```

### 8.5 감사 체인 무결성 검증 실패

```bash
uv run aegis verify-audit
# [verify-audit] BROKEN @ idx N (chain hash mismatch)
```

원인: 누군가 `~/.aegis/audit.jsonl`을 직접 수정했을 가능성. 정상적인 hook 동작에선 발생하지 않음.

조치: 해당 인덱스의 레코드를 `head -n N+1 audit.jsonl | tail -1`로 확인 → 의심 활동 추적. 깨끗한 시작이 필요하면 `mv ~/.aegis/audit.jsonl ~/.aegis/audit.jsonl.broken.$(date +%s)`로 격리 후 새 체인 시작.

---

## 9. 선택 강화 (필요해질 때)

### 9.1 Anthropic Haiku judge ($, 의미적 판정)

```bash
# .env에 키 입력
ANTHROPIC_API_KEY=sk-ant-...
AEGIS_JUDGE_PROVIDER=haiku
```

```bash
uv run aegis install --mode local --judge haiku    # hook env 갱신
```

비용: ~$0.001/call. 정확도가 가장 높지만 매 tool마다 네트워크 round-trip.

### 9.2 On-device Phi-3.5 sLLM (오프라인, +3GB RAM)

```bash
uv sync --extra local-llm                          # llama-cpp-python (Metal 가속)
uv run aegis pull-model phi-3.5                    # ~2GB GGUF
uv run aegis sidecar start --model phi-3.5         # PR #30 daemon, cold-load 6.5s → 1.5s
uv run aegis install --mode local --judge hybrid   # M13 + Phi cascade
```

`aegis sidecar status` / `aegis sidecar stop`으로 lifecycle 관리.

### 9.3 Sidecar (Docker) 모드 — 풀 트랜잭션 추적

Plugin mode에서는 ATMU 2PC가 동작하지만 다음은 sidecar에서만:

- `/forensic/replay` Merkle + AES-GCM journal 검증
- M12 Cost Attestation Ledger (Ed25519 signed cost 기록)
- HAM (M16) recall + ground operations
- 멀티 테넌트 격리

```bash
brew install --cask orbstack
open -a OrbStack                                   # 첫 실행 권한 승인
docker compose up -d --build
until curl -sf localhost:8000/healthz; do sleep 1; done
bash tools/test_hook.sh                            # 10 sidecar smoke test
uv run aegis install --mode sidecar                # hook을 HTTP POST로 전환
```

전 흐름 자동화: `bash tools/setup_macmini.sh` (자세한 내용은 [`SETUP_MACMINI.md`](../SETUP_MACMINI.md)).

### 9.4 BGE 임베딩 (의미적 ATV)

dummy embedding은 SHA3 결정적 노이즈 → 의미적 유사도 없음. RAG / case memory / session drift를 정확히 쓰려면 BGE-base-en (~100MB):

```bash
uv sync --extra local-llm
uv run aegis pull-model bge-base-en
uv run aegis install --mode local --judge hybrid --embedding bge-local
```

---

## 10. 환경 변수 레퍼런스

| Env | 기본값 | 설명 |
|---|---|---|
| `AEGIS_TENANT_ID` | `claude-code-local` | 매 record의 tenant 태그 |
| `AEGIS_LOCAL_AUDIT` | `~/.aegis/audit.jsonl` | 감사 체인 파일 |
| `AEGIS_INTENT_LOG_DB` | `~/.aegis/intent_log.sqlite` | ATMU 2PC WAL |
| `AEGIS_SHADOW_LOG` | `~/.aegis/shadow.jsonl` | M11 burn-in shadow corpus |
| `AEGIS_BURNIN_SHADOW` | `0` | `1` → shadow 기록 켬 |
| `AEGIS_HW_PROVIDER` | `none` | `sim` → HW 시뮬레이터, `real` → 실제 PMU/EDAC/NVML (T3) |
| `AEGIS_HW_INJECT_ATTACK` | `(empty)` | sim 모드 데모용 attack injection |
| `AEGIS_APPROVE_AS_BLOCK` | `1` | `0` → REQUIRE_APPROVAL이 경고만 출력하고 통과 |
| `AEGIS_ATMU_DISABLE` | `0` | `1` → ATMU 2PC 비활성, audit chain만 동작 |
| `AEGIS_JUDGE_PROVIDER` | `dummy` | `dummy` / `hybrid` / `local-phi` / `haiku` |
| `AEGIS_EMBEDDING_PROVIDER` | `dummy` | `dummy` / `bge-local` / `openai` |
| `AEGIS_HOOK_VERBOSE` | `0` | `1` → ALLOW도 stderr에 출력 |
| `AEGIS_POLICY_DIR` | `./policies` | step310 sensitive_paths.json 경로 |

---

## 11. 빠른 명령 치트시트

```bash
# 설치 / 갱신
uv run aegis install --mode local --judge dummy        # 신규
uv run aegis install --mode local --judge hybrid --force # 갱신
uv run aegis uninstall                                  # 제거 + 백업 복원

# 상태 확인
uv run aegis status                                    # plugin overview
uv run aegis burnin shadow-status                      # 학습 corpus 진행도
uv run aegis verify-audit                              # 감사 체인 무결성
uv run aegis report                                    # 5줄 daily report
uv run aegis report --explain <trace>                  # layer-by-layer "왜"

# 학습 사이클
uv run aegis burnin train-m13 --corpus ~/.aegis/shadow.jsonl
uv run aegis burnin compare-m13
mv models/m13_attribution_head_v2.json models/m13_attribution_head_v1.json

# 검증
./scripts/dogfood_check.sh                             # 8 hook 검증
./scripts/macmini_user_test.sh                         # 7 incident 회귀
./scripts/macmini_user_test.sh --hybrid                # hybrid 모드 회귀

# sLLM daemon
uv run aegis sidecar start --model phi-3.5
uv run aegis sidecar status
uv run aegis sidecar stop

# 모델 다운로드
uv run aegis pull-model phi-3.5
uv run aegis pull-model bge-base-en
uv run aegis pull-model llama-3.2-1b
```

---

## 12. 1주일 운영 체크리스트

- **Day 0** — 1~5장 (설치 + 검증)
- **Day 1~3** — 자연스럽게 Claude Code 사용. 매일 `aegis status` + `aegis burnin shadow-status` 한 번. 200+ records 목표.
- **Day 4** — 첫 train-m13 + compare-m13. v2 reject 흔함 (정상). 데이터 더 모음.
- **Day 5~7** — 추가 데이터 + macmini_user_test로 공격 사례 보강. v2가 v1을 이기는 시점 도달.
- **Day 7+** — v2 채택 → hybrid 복귀. 이때부터 hook이 본인 트래픽에 맞춰 calibrate된 상태로 운영.

매월 1회: `aegis verify-audit` 전체 체인 + `aegis snapshots prune --older-than 30d` 정리.

---

문서 끝. 이슈는 [https://github.com/happyikas/Aegis-ATV/issues](https://github.com/happyikas/Aegis-ATV/issues) 에 보고.
