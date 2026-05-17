# Aegis ATV — 사용 설명서

> 문서 버전: **v0.7.0** (2026-05-17) · Aegis ATV · 한국어 (정본)
>
> **이 문서가 누구를 위한 것인가요?** 코드를 잘 모르는 분도 5–10분 안에 "Aegis 가 무엇이고, 어떻게 쓰는지" 이해할 수 있게 작성한 통합 가이드입니다. 기능별 깊은 매뉴얼은 [`docs/manuals/`](manuals/README.md) 에 있습니다.

---

## 1. 한 페이지 요약

### 무엇을 하는 도구입니까?

**Aegis 는 AI 에이전트가 실수 (또는 공격) 로 시스템을 망가뜨리지 못하게 막고, 동시에 모든 행동을 위조 불가능한 기록으로 남기는 도구입니다.**

비유로 설명하면:
- 🚪 **자물쇠** — AI 에이전트가 위험한 명령을 실행하기 직전에 막습니다
- 📹 **CCTV** — AI 에이전트의 모든 행동을 시간 순서대로 기록합니다
- 🧾 **공증된 영수증** — 그 기록은 암호로 서명되어 있어 사후에 누구도 위조 못 합니다
- 🤖 **반복 승인 자동화** — 안전했던 패턴을 학습해 매번 묻지 않고 통과 (v0.5+, 6 단 안전망)
- 📚 **자기 학습 위키** — agent 활동을 의미 검색 가능한 wiki 로 정리 (v0.6.0)

### 어디에 끼어드나요?

```
  ┌──────────────┐
  │  AI 에이전트  │   ← Claude Code, OpenClaw, Codex, 자체 봇 …
  └──────┬───────┘
         │ "이 도구를 호출하겠다"
         ▼
  ┌──────────────┐
  │  ★ Aegis     │   ← 자물쇠 + CCTV + 영수증
  │              │     "이 행동 안전한가?" → 통과 / 승인 요청 / 차단
  └──────┬───────┘
         │ (안전한 경우만)
         ▼
  ┌──────────────┐
  │  실제 도구    │   ← 셸 명령, DB 조회, API 호출, 결제 …
  └──────────────┘
```

이 위치를 PitchDeck 에서는 **"below the model — between decision and execution"** 이라고 부릅니다. 모델의 안전 응답 필터 (예: Claude 의 답변 거부) 보다 **한 단계 더 아래** 에서 작동합니다.

### 한 줄 설치

```bash
uv run aegis install --mode local
```

→ 이 한 줄이면 Claude Code 가 만드는 모든 도구 호출이 Aegis 를 거칩니다. 무료, 외부 호출 0, 데이터는 노트북 밖으로 안 나갑니다.

---

## 2. 어떤 사람에게 필요한가요?

| 당신은 | Aegis 가 해주는 것 |
|---|---|
| 🧑‍💻 **Claude Code 일상 사용 개발자** | "삭제하지 말아야 할 파일을 AI 가 지우려 할 때" 자동 차단 + 모든 작업의 자동 기록 |
| 🏥 **병원 / 금융 / 정부 등 규제 산업** | EU AI Act / HIPAA / SOC 2 가 요구하는 **변조 불가능한 감사 로그** 제공 |
| 🤖 **AI 에이전트를 만드는 개발자** | OpenClaw / 자체 프레임워크에 보안 + 감사 layer 를 코드 한 줄로 추가 |
| 🛡️ **기업 보안 / 컴플라이언스 팀** | 여러 AI 도구 × 여러 LLM provider 의 행동을 **하나의 대시보드** 에서 비교 |
| 🚀 **multi-LLM 환경 운영자** | OpenRouter / 멀티 provider 환경에서 *어느 provider 가 더 위험한지* 정량 측정 |

---

## 3. 한 시나리오로 보는 동작 — "삭제 사고 방지"

**상황**: 당신이 Claude Code 에게 "tmp 폴더 좀 정리해줘" 라고 부탁. AI 가 잘못 해석해서 시스템 폴더를 통째로 삭제하려고 함.

**Aegis 없이**:
```
사용자: "tmp 정리해줘"
Claude:  → 위험한 재귀 삭제 명령 실행 (시스템 폴더 대상)
         → 실행됨, 시스템 망가짐
```

**Aegis 와 함께**:
```
사용자: "tmp 정리해줘"
Claude:  → 같은 위험 명령 시도 → Aegis 가 가로챔
Aegis:   ⛔ BLOCK  trace=abc123  (45ms)
         reason: dangerous pattern: 시스템 경로 대상 재귀 삭제
         advise: [HIGH] security-reviewer — Block until reviewer ACKs
사용자:  (안 망가짐. audit log 에 시도 기록됨.)
```

이 한 동작에서:
- ⚡ **45ms 안에 차단** — 사용자는 "AI 가 느려진" 느낌 없음
- 📝 **audit log 에 영구 기록** — 사후 분석 가능
- 🤖 **8 명의 가상 advisor** 가 자동으로 권고 — `security-reviewer`, `cost-optimizer`, `loop-breaker` 등 위험 영역별 전문 조언

> 💡 *이 문서를 작성하는 동안 실제로 Aegis 의 firewall 이 작동했습니다. 이 섹션의 처음 예시는 destructive pattern 정규식에 걸려 문서 저장이 차단되었습니다 — Aegis 가 자기 자신을 안전하게 보호하는 증거.*

---

## 4. 5분 설치 — 3가지 옵션 중 하나

### 옵션 A — Solo 개발자 (가장 간단)

```bash
# 1) 저장소 클론 + 의존성
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
uv sync

# 2) Claude Code 에 Aegis 후크 설치
uv run aegis install --mode local

# 3) Claude Code 재시작 — 끝
```

이게 PitchDeck 의 **"Solo Free"** 입니다. 비용 0원, 외부 호출 0, 데이터는 본인 머신 안에서만.

### 옵션 B — 한 줄 설치 (저장소 클론 없음)

```bash
curl -LsSf https://raw.githubusercontent.com/happyikas/Aegis-ATV/main/scripts/install.sh | bash
```

### 옵션 C — Mac 사용자 (Homebrew)

```bash
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install happyikas/aegis/aegis
aegis install --mode local
```

### 옵션 D — 기업 / 멀티 사용자 (Sidecar 모드)

여러 사람이 같은 Aegis 인스턴스를 사용해야 하는 환경:

```bash
docker compose up -d
# → localhost:8000 에서 FastAPI 서비스 가동
# → 모든 클라이언트가 이 service 를 통해 검증
```

---

## 5. 처음 7가지 명령어

설치 후 가장 먼저 익혀야 할 일곱 가지:

| 명령 | 설명 | 언제 쓰나 |
|---|---|---|
| `aegis status` | 현재 설치 상태 + 운영 통계 | 매일 첫 명령 |
| `aegis report` | 최근 24시간의 5줄 위험 요약 | 매일 / 매주 점검 |
| `aegis doctor` | **Cost · Performance · Security 통합 markdown 리포트** | 주간 / 월간 종합 |
| `aegis verify-audit` | 감사 체인이 위조되지 않았는지 검증 (1초) | 사건 의심 시 / 매주 |
| `aegis forensic last` | 가장 최근 BLOCK / REQUIRE_APPROVAL 케이스 자세히 보기 | "왜 차단됐지?" |
| `aegis advise` | AI 가 권고한 행동 (cost / security / performance) 종합 | 운영 개선 |
| `aegis autonomy show -v` | 학습된 신뢰 패턴 + 자동 승인 상태 (§6.5) | 매일 / 매주 |
| `aegis knowledge search <q>` | wiki 의미 검색 (§7-2) | 디버깅 / 학습 |

### 예시 — `aegis report` 출력

```
$ aegis report

🛡️  Aegis Risk Report  (last 24h)

  Calls: 1,243   ALLOW 1,198 / APPROVAL 38 / BLOCK 7
  Top risks:
    • 4× destructive-bash (production folder)
    • 2× credential-leak attempts
    • 1× cost spike (Claude → GPT-4 retry loop)

  Cost: $4.18 (Claude $3.92 + GPT-4 $0.26)
  Audit chain: ✓ intact, 12 sessions

  next: `aegis forensic last` to inspect the latest BLOCK
```

5 줄로 "어제 무슨 일이 있었나" 파악 — PitchDeck 에서 강조한 *Observability* 부분의 일상 사용 화면.

---

## 6. 4가지 핵심 기능 — Coach · Live · Doctor · Autonomy

Aegis 는 PitchDeck 의 5 가지 기반 기술 (ATV / ATMU / sLLM / Crypto-Sign / Burn-in) 을 **4 개 사용자 기능** 으로 묶어 제공합니다. 각각 한 문단 + 깊은 매뉴얼 link. Autonomy 는 v0.5.11+ 에서 추가된 4 번째 기능 (Coach 의 학습 결과를 자동 승인에 연결).

### 🏋️ ATV Coach — "내 환경의 정상 / 이상 학습"

당신 환경의 **정상적인 도구 사용 패턴**을 5단계 × 4 phase 로 자동 학습합니다. 처음에는 *Observation* 단계 (관찰만), 충분히 모이면 *Shadow → Assisted → Production* 으로 점진 승격. 학습된 baseline 이 sLLM judge 에 주입되어 "이 도구 호출이 평소와 다른지" 빠르게 판단.

```bash
aegis coach burnin status      # 학습 진행도
aegis coach burnin train-m13   # baseline 재학습
aegis baseline reattest        # baseline 갱신 적용
# (v0.4.x 호환: `aegis burnin ...` 그대로 동작)
```

→ [`docs/manuals/COACH_MANUAL.ko.md`](manuals/COACH_MANUAL.ko.md)

### 📊 ATV Live — "지금 무슨 일이 벌어지나"

agent 의 **Cost / Performance / Security** 를 실시간으로 추적합니다. "이번 주 비용 분석", "어떤 agent 가 가장 위험한가", "어떤 LLM provider 가 BLOCK rate 가 높은가" 같은 질문에 답합니다.

```bash
aegis live                                  # 한 화면 TUI (cost · perf · security)
aegis report                                # 5 줄 요약
aegis report --by-aid                       # agent 별 분리
aegis report --by-provider                  # LLM provider 별 (Claude vs GPT)
aegis report --by-aid-and-provider          # 교차 분석
aegis cost summary --since 7d               # 주간 비용
aegis fleet-monitor start                   # 알림 daemon (Slack/ntfy)
```

→ [`docs/manuals/LIVE_MANUAL.ko.md`](manuals/LIVE_MANUAL.ko.md)

### 🔧 ATV Doctor — "사건이 일어났을 때 진단 + 치료"

뭔가 잘못됐을 때 **무엇이 일어났는지 정확히 재현** + **다음에 어떻게 막을지 권고** + **시간 되돌리기**.

```bash
aegis doctor                       # Cost · Performance · Security 통합 리포트
aegis doctor --since 24h           # 윈도우 지정
aegis doctor --out report.md       # 마크다운 파일로 저장
aegis forensic last                # 최근 BLOCK 분석
aegis advise <trace_id>            # 그 케이스의 advisor 권고
aegis rollback <trace_id>          # 그 시점으로 시스템 상태 되돌리기
aegis verify-audit                 # 감사 체인 무결성 검증
```

→ [`docs/manuals/DOCTOR_MANUAL.ko.md`](manuals/DOCTOR_MANUAL.ko.md)

### 🤖 ATV Autonomy — "반복되는 승인을 자동화" (v0.5.11–v0.5.27)

매번 같은 `REQUIRE_APPROVAL` 에 일일이 응답하는 피로를 줄이는 기능입니다. 과거 audit 데이터에서 **반복적으로 안전했던 패턴**을 학습해, 이후 동일 패턴이 발생하면 자동으로 통과시킵니다. 모든 자동 승인은 audit chain 에 `aegis.autonomy.step331.run` 으로 영구 기록 — 추적 가능성은 잃지 않습니다.

**기본 OFF.** 활성화하려면 `AEGIS_AUTONOMY_ENABLED=1` (§11 의 hook 환경변수 또는 `~/.zshrc`).

#### 6 단 안전망 — irreversible 은 절대 자동 승인 안 됨

자동 승인 결정은 다음 6 개 게이트를 **모두** 통과해야 합니다 (하나라도 실패하면 사람에게 escalate):

1. **Reversibility 분류기** — 시스템 root 재귀 삭제 등 irreversible action 은 신뢰도와 무관하게 거부 (정책: `policies/reversibility.json`, 23 룰, 4 레벨 trivial/reversible/costly/irreversible).
2. **Drift 감지** — 학습 시점 분포와 현재 분포의 Jensen-Shannon divergence 가 threshold 초과면 거부.
3. **ATV centroid (Mahalanobis)** — 학습된 centroid 에서 (log_cost, log_tokens, log_latency) 3-D 공간으로 > 3σ 떨어지면 거부.
4. **Andon tripwire** — 연속 N 회 자동 승인 후 다음 한 건은 강제로 사람에게 (기본 20, `AEGIS_AUTONOMY_ANDON_THRESHOLD`).
5. **Session-prior** — 운영자가 라벨한 위험도가 `min_trust` 를 스케일 (exploring=0.70 / refactor=0.85 / prod-deploy=0.95, 8h TTL).
6. **ε-greedy** — 5% 는 강제로 사람에게 보내 self-confirming loop 방지 (`AEGIS_AUTONOMY_EPSILON=0.05`).

추가로 **Bayesian backbone**: Beta(α, β) posterior + LCB decision rule + 30-day exponential decay + 80/20 train/val split 의 ECE 0.10 calibration gate + Bonferroni-adjusted `min_samples` — 7 가지 ML 부작용 (overfit / self-confirm / spurious / staleness / drift / sparsity / 다중비교) 을 동시 차단.

#### 일상 명령어

```bash
# 일주일 운영 후 trust table 학습 (read-only, 멱등)
aegis autonomy learn --since 7d

# 학습 결과 확인
aegis autonomy show -v
# 출력 예시:
#   tool   signature        n     LCB   drift
#   Bash   loop:Bash        290   1.00  0.011
#   Bash   cost-divergence  116   0.99  0.009

# 작업 모드별 위험도 라벨 (8 시간 TTL 자동 만료)
aegis autonomy session start exploring    # POC / 실험: min_trust 0.70
aegis autonomy session start refactor     # 기본: min_trust 0.85
aegis autonomy session start prod-deploy  # 릴리스: min_trust 0.95
aegis autonomy session status              # 현재 라벨 / 만료
aegis autonomy session end                 # 라벨 해제

# 특정 trace 의 자동 승인 결정 사후 분석 (11-gate forensic walker)
aegis autonomy explain <trace_id>
# → 게이트별 PASS/FAIL/INFO 표 + 최종 결과 (would-bypass / would-refuse / not-eligible)

# 잘못 승인된 trace 명시적 거부 (재학습 시 가중치 -10β)
aegis autonomy deny <trace_id> --note "사유"

# 자동 승인 직후 BLOCK 된 anomaly 추적
aegis autonomy outliers --since 7d
```

#### `aegis doctor` 통합

`aegis doctor --since 7d` 출력에 🤖 Autonomy 섹션이 자동 포함됩니다 (bypass count, ε-greedy explore count, outlier 표). 사용자가 활성화하지 않았다면 한 줄 "no data" 만.

→ 자세히: [`CHANGELOG.md`](../CHANGELOG.md) §0.5.11 – §0.5.27

### 🔗 Claude Code Agent View 시너지 (v0.7.0)

Aegis 는 Claude Code 의 hook 시스템 위에 올라타지만, v0.6 까지는 단방향이었습니다 (Aegis → tool call 차단). v0.7.0 부터는 **양방향** — Claude Code 가 노출하는 transcript / subagent metadata / 6-hook lifecycle 를 Aegis 가 읽어 자기 데이터와 cross-reference 합니다.

#### `aegis subagent-graph` — Task 호출별 verdict mix

Claude Code 의 ``~/.claude/projects/<encoded-cwd>/<session>.jsonl`` transcript 에는 모든 `Task` tool 호출 (Explore / Plan / general-purpose / …) 의 metadata 가 있고, Aegis audit chain 에는 그 시간대의 verdict 가 있습니다. 둘을 합치면 "어느 subagent 가 가장 자주 BLOCK 을 유발하나" 같은 질문에 답이 됩니다.

```bash
$ aegis subagent-graph
Subagent graph — session 58ed2cfc (33431 transcript · 11326 audit records)
  ├─ Task → Explore  "Map current product surface"  (1.6m)
  │  verdicts: ALLOW 0 · APPROVAL 35 · BLOCK 0
  │  tools:    Bash×44, Read×23, Agent×2
  ├─ Task → Explore  "Map sLLM advice + burn-in"  (1.5m)
  │  verdicts: ALLOW 0 · APPROVAL 33 · BLOCK 0
  └─ Task → Explore  "Map ATV+audit write points"  (1.1m)
     verdicts: ALLOW 0 · APPROVAL 28 · BLOCK 0
```

옵션:
* `--transcript PATH` — 다른 세션 transcript 지정 (기본: cwd 의 최신)
* `--audit PATH` — audit chain 경로 override
* `--json` — 기계 가독 형식

`AEGIS_CLAUDE_PROJECTS_DIR` 로 Claude Code 의 projects 디렉토리 경로 override 가능 (비표준 설치).

#### SessionStart 상태 banner — 매 세션 1줄 요약

v0.7.0 부터 새 Claude Code 세션을 시작할 때마다 다음과 같은 한 줄이 stderr 로 출력됩니다:

```
🛡️  Aegis · 11,924 audit records · 7 BLOCKs in 24h · autonomy: 2 pattern(s) learned
```

이전에는 첫 설치 직후만 환영 메시지가 떴고 그 후엔 silent 였습니다. v0.7.0 의 매 세션 banner 는 "Aegis 가 살아있고, 24h 위협 분포가 어떻고, 자동화가 어디까지 학습됐나" 를 0.1 초 안에 알려줍니다.

* `AEGIS_SESSION_BANNER=brief` (기본) — 한 줄 banner.
* `AEGIS_SESSION_BANNER=off` — legacy silent 모드.
* `AEGIS_SESSION_BANNER=full` — 첫 세션 환영을 강제 재출력.
* `AEGIS_WELCOME_DISABLE=1` — 전부 silent (banner + 환영).

#### 6 개 hook lifecycle — 전 표면 활용

Claude Code 가 노출하는 6 개 hook event 와 Aegis 의 역할:

| Hook | Aegis 역할 |
|---|---|
| **PreToolUse** | firewall pipeline (16-step) + autonomy bypass + ATMU 2PC phase 1 |
| **PostToolUse** | ATMU 2PC commit + audit chain append |
| **Stop** | transcript 의 토큰 비용 백필 |
| **PreCompact** (v0.7.0 활용) | 컨텍스트 압축 직전 ATV 상태 snapshot (forensic 경계) |
| **UserPromptSubmit** (v0.7.0 활용) | 사용자 prompt retry 감지 (privacy-safe Jaccard / BGE cosine) |
| **SessionStart** (v0.7.0 enrich) | 위의 1 줄 banner |

이전엔 PreToolUse / PostToolUse / Stop 만 등록됐고 (3 개), v0.4.1 의 deprecation cleanup 으로 일부 hook 이 의도치 않게 사라지는 회귀가 있었습니다. v0.7.0 시점에 6 개 전체가 정상 등록 — 본인 환경 확인은:

```bash
uv run aegis install --target claude-code --mode local --force
# 또는
python3 -c "import json, pathlib; d = json.loads((pathlib.Path.home() / '.claude/settings.json').read_text()); print(sorted(d.get('hooks', {})))"
# → ['PostToolUse', 'PreCompact', 'PreToolUse', 'SessionStart', 'Stop', 'UserPromptSubmit']
```

> ⚠️ `--force` 는 hook 명령을 새로 작성하므로 사용자가 수동 추가한 env var (예: `AEGIS_AUTONOMY_ENABLED=1`) 가 사라집니다. 재설치 후 본인 custom env 를 다시 prepend 해야 합니다.

→ 자세히: [`CHANGELOG.md`](../CHANGELOG.md) §0.7.0

---

## 7. PitchDeck 의 5 기술 — 코드 위치 매핑

| PitchDeck 명칭 | 무엇인가 (한 줄) | 코드 위치 |
|---|---|---|
| **ATV** (Agent Telemetry Vector) | 모든 agent 행동을 2,048-D 벡터로 인코딩 — 30 개 명시 subfield + 학습된 임베딩 | `src/aegis/schema.py` |
| **ATMU** (Action Trust Management Unit) | 도구 실행 전에 정책을 적용하는 게이트 — 16 단계 firewall | `src/aegis/firewall/step3*.py` |
| **sLLM Judgment Engine** | 양자화된 3B 로컬 모델이 ambiguous case 에 대한 second opinion 제공 — 0 cloud egress | `src/aegis/judge/`, `src/aegis/firewall/step340.py` |
| **Crypto-Sign** | SHA3 hash chain + Ed25519 서명 — 1 명령으로 외부 감사 가능 | `src/aegis/audit/`, `aegis verify-audit` |
| **Burn-in** | 매 릴리스 1k+ 적대적 시나리오 재현 — 서명된 scorecard 없이 빌드 안 나감 | `src/aegis/burnin/` |

비전문가용 한 문장으로 통합:
> **5 가지 기술이 합쳐서**: agent 의 모든 행동을 **표준 벡터** 로 인코딩하고 (ATV), 그 벡터에 **정책을 적용** 하고 (ATMU), 애매한 케이스는 **로컬 AI 가 검토** 하고 (sLLM), 모든 결정을 **암호로 서명된 체인** 에 기록 (Crypto-Sign), 매 릴리스마다 **공격 시나리오로 재검증** (Burn-in).

---

## 7-1. ContextMemory — 분석 fast-path (CXL/Computational SSD emulation)

PitchDeck 의 *"HARDWARE NEXT — Near-storage / GPU-resident accelerator. Same ATV schema is the silicon spec"* 에 매핑되는 software emulation 입니다.

```
~/.aegis/audit.jsonl         ← SHA3 + Ed25519 체인 (변조 증거)
~/.aegis/context_memory.jsonl ← ATV 분석 fast-path (이번 추가)
```

ATV 가 생성될 때마다 (= 모든 tool call) Aegis 가 두 파일에 동시에 기록합니다:

- **audit.jsonl** — 감사 체인 (보존). 변조 시 `aegis verify-audit` 가 즉시 fail.
- **context_memory.jsonl** — 분석 store. 같은 정보를 *denormalized 분석 친화 형태*로. 미래에는 이 파일이 CXL SSD / Computational SSD 의 near-storage compute 입력이 됨 (그래서 schema 가 silicon-ready).

**`aegis doctor`** 가 이 store 를 읽어 **markdown 리포트**를 만듭니다 (`💰 Cost · ⚡ Performance · 🛡️ Security` 3 축 + 각 축의 heuristic 권고):

```bash
aegis doctor                       # 최근 7일 (기본) → stdout
aegis doctor --since 24h           # 윈도우 지정
aegis doctor --out report.md       # 파일 저장
aegis doctor --context-memory /path/to/cm.jsonl   # 경로 override
```

리포트 샘플 발췌:

```markdown
# Aegis Doctor Report

**기간**: 최근 7.0 일

## 📊 요약
- 총 ATV: 1,243
- Decision 분포: ALLOW 96.4% · REQUIRE_APPROVAL 3.1% · BLOCK 0.5%

## 💰 Cost
- 총 비용: $4.18
- Provider 별 (비용 desc):
  | Provider | 호출 수 | 총 비용 |
  |---|---:|---:|
  | `openrouter:anthropic-claude-sonnet-4` | 842 | $3.92 |
  | `openrouter:openai-gpt-4o-mini`        | 401 | $0.26 |

### 권고
- 🔴 **anthropic-claude-sonnet-4 가 비용의 94% 차지** → `aegis report --by-provider` 로 상세 확인 후 저비용 provider 라우팅 검토

## ⚡ Performance
- p50 4.2 ms · p95 47 ms · p99 134 ms · max 312 ms

### 권고
- 🟢 **p95 47 ms — PitchDeck 의 < 50 ms 약속 충족 ✓**

## 🛡️ Security
- BLOCK rate 0.50% (baseline 0.3-1.0% 안)
- step310 (destructive bash) 가 BLOCK 의 57% 차지

### 권고
- 🟡 **step310 가 BLOCK 의 57%** → policies/safe_actions.json 검토
```

ContextMemory 의 store path / schema 는 `AEGIS_CONTEXT_MEMORY_PATH` 환경 변수로 override 가능. Solo Free 기본 동작이라 **별도 설정 없이 자동으로 채워집니다** — Aegis 가 한 번이라도 작동한 후 `aegis doctor` 만 치면 첫 리포트 출력.

---

## 7-2. LLM-Wiki Knowledge Layer (v0.5.15–v0.6.0)

ContextMemory 의 raw JSONL 위에 **wiki 형태의 derived 지식 베이스**를 만들어 sLLM advisor 가 소비하게 합니다. raw store 가 audit / replay 책임이라면, wiki 는 *sLLM-ready 한* 형태로 가공된 view 입니다.

각 entity (agent / tool / pattern) + event (session / incident / workflow) 가 self-contained "wiki 기사"로 저장됨:

```
~/.aegis/knowledge/
  index.json                       ← 카탈로그
  agent_demo-session-2026-05.json
  tool_Bash.json
  pattern_Bash:loop:Bash.json
  incident_sess-block-stderr_*.json
  ...
```

각 entry: 1-2 문장 lead summary + infobox (key-value 표) + ordered sections + 교차 참조 + tags + n_observations + confidence.

#### 6 가지 entry kind

| Kind | 무엇 | 예 |
|---|---|---|
| **agent** | aid 별 활동 프로파일 | `agent/demo-session-2026-05` |
| **tool** | tool 별 사용 통계 + verdict mix | `tool/Bash` — 5,796 invocations, ALLOW 48% / REQUIRE_APPROVAL 34% / BLOCK 18% |
| **pattern** | (tool, reason) 별 반복 패턴 | `pattern/Bash:loop:Bash` — loop detector 발동 290 회 |
| **session** | 30 분 gap 으로 segment 된 작업 버스트 | — |
| **incident** | 1 개 BLOCK + 직전 3 콜 + 직후 2 콜 컨텍스트 | `incident/sess-block-stderr/...` |
| **workflow** | agent 별 ≥ 3 회 반복 tool bigram | — |

#### 일상 명령어

```bash
# 매주 1 회 wiki 재빌드
aegis knowledge build --since 30d

# 카탈로그 / 단일 entry
aegis knowledge list --kind tool --limit 10
aegis knowledge show tool/Bash
aegis knowledge show pattern/Bash:loop:Bash

# 의미 검색 — TF-IDF (기본, pure-Python, deterministic)
aegis knowledge search "loop bash" -k 3

# 의미 검색 — embedding (v0.6.0, paraphrase 잡힘)
aegis knowledge search "expensive bash invocations" --engine embedding -k 3
# → AEGIS_EMBEDDING_PROVIDER=bge-local 일 때 lexical overlap 없는 의미 일치도 surface
# → dummy provider 라도 deterministic SHA3 vector 라 CI / 베이스라인용으로 사용 가능

# advisor 에 wiki context 자동 주입
export AEGIS_ADVISOR_USE_KNOWLEDGE=1
# → sLLM advisor 호출마다 agent 별 600–1,000 토큰의 wiki 컨텍스트가 자동 합쳐짐
```

> 💡 **검색 엔진 선택**: `--engine tfidf` (기본) 는 키워드 매칭 — 빠르고 ML 의존성 0. `--engine embedding` 은 의미 일치 — `"비용 발산 bash"` 가 `"cost-divergence on Bash"` 를 surface. 두 엔진 모두 mtime-cached, hot-path safe (실패 시 빈 결과 반환, 절대 raise 안 함).

#### 사후 검증 / 측정

```bash
aegis knowledge measure <aid>            # wiki 활성 vs 비활성 토큰 delta
aegis knowledge advisor-context <aid>    # 실제 주입되는 컨텍스트 dump (디버깅)
```

→ 자세히: [`CHANGELOG.md`](../CHANGELOG.md) §0.5.15 – §0.6.0

---

## 8. 요금제 — Solo Free vs Pro vs Team vs Enterprise

PitchDeck 의 commercial offering boundary 와 매칭:

| | **Solo Free** | **Pro** | **Team** | **Enterprise** |
|---|---|---|---|---|
| 가격 | **무료 (영구)** | $19/월 | $39/seat/월 | 별도 |
| 라이선스 | Apache-2.0 | 상용 | 상용 | 상용 |
| 16-step Firewall | ✅ | ✅ | ✅ | ✅ |
| 감사 체인 | ✅ | ✅ | ✅ | ✅ |
| 8-advisor pipeline (Coach + Live + Doctor) | ❌ (advisor OFF) | ✅ | ✅ | ✅ |
| sLLM judge (로컬 Phi-3) | ❌ (dummy 룰만) | ✅ | ✅ | ✅ |
| Haiku judge (cloud) | ❌ | ✅ | ✅ | ✅ |
| Sidecar (멀티 사용자) | ❌ | ❌ | ✅ | ✅ |
| 우선 지원 + SLA | ❌ | ❌ | ❌ | ✅ |

핵심: **무료 tier 도 단독으로 의미 있게 작동** 합니다. 본인 노트북, 본인 데이터, 외부 호출 0. PitchDeck 의 "Solo Free unconditionally free forever (Apache-2.0)" 약속.

```bash
# 라이선스 활성화 (Pro 이상)
aegis license activate ~/Downloads/my-key.jwt

# 현재 상태 확인
aegis license status
```

→ 자세히: [`PRICING.md`](../PRICING.md) + [`docs/LICENSE_KEY.md`](LICENSE_KEY.md)

---

## 9. 통합 시나리오 — "어떤 도구 / 환경에서 쓰나"

### Claude Code 사용자 (가장 흔함)

```bash
uv run aegis install --mode local --profile pro
# → ~/.claude/settings.json 자동 패치
```

이후 Claude Code 가 만드는 모든 tool call (Bash / Edit / Read / MCP / …) 이 Aegis 를 거칩니다. 사용자 추가 작업 없음.

### OpenClaw 사용자 (멀티 채널 agent — Telegram/Discord/Slack/CLI)

OpenClaw 프로젝트에서:

```bash
npm install @happyikas/openclaw-plugin-aegis
```

```typescript
// plugins/aegis/index.ts
import { activate } from "@happyikas/openclaw-plugin-aegis";
export default activate;
```

Aegis sidecar 가 띄워져 있으면 모든 OpenClaw tool call 이 자동 검증.

→ 자세히: [`docs/integrations/openclaw.md`](integrations/openclaw.md)

### OpenRouter 사용자 (300+ models, 60+ providers)

```python
from openai import OpenAI
from aegis.integrations.openrouter import canonical_provider

client = OpenAI(base_url="https://openrouter.ai/api/v1", ...)
resp = client.chat.completions.create(model="anthropic/claude-sonnet-4", ...)

# Aegis 에 OpenRouter 의 실제 provider 전달
provider_str = canonical_provider(resp.model_dump())
# → "openrouter:anthropic-claude-sonnet-4"
```

이렇게 하면 `aegis report --by-provider` 가 OpenRouter route 별로 분리됩니다 — 같은 prompt 가 OpenAI 로 갔을 때 BLOCK rate 가 Claude 와 다르면 자동 경고.

→ 자세히: [`docs/integrations/openrouter.md`](integrations/openrouter.md)

### Hermes 사용자 (self-improving agent)

Hermes 처럼 스스로 학습 / 개선하는 agent 환경에서 Aegis 는 **외부 감사 layer** 역할:

> *"agent 가 자기 코드/skill 을 갱신할 때, 그 갱신이 안전한 방향인지 누가 검증합니까?"* — Aegis 가 그 외부 검증입니다.

→ 자세히: [`docs/integrations/hermes.md`](integrations/hermes.md)

---

## 10. 자주 묻는 질문

### Q1. 내 데이터가 외부로 나가나요?

**Solo Free / Pro / Team 모두**: `--mode local` 사용 시 **0 byte 가 외부로 안 나갑니다**. Aegis 사이드카는 localhost 에서만 작동, 감사 로그도 본인 머신의 `~/.aegis/audit.jsonl` 에만 저장.

`--profile pro / cloud` 의 경우 *애매한 케이스만* Haiku judge 에 보낼 수 있는데, 이건 OPT-IN 이고 `AEGIS_JUDGE_PROVIDER=local-phi` 로 끌 수 있습니다.

규제 산업: `--mode local --profile pro` + `AEGIS_JUDGE_PROVIDER=local-phi` = **외부 호출 영구 0**.

### Q2. 너무 느리지 않을까요?

Solo Free: 평균 5ms (dummy 룰), 95% latency < 10ms.
Pro / Team: 평균 50ms (sLLM judge 포함, p95).
PitchDeck 의 *"< 50ms p95 — agent never feels it"* 와 매칭.

### Q3. Claude Code 가 이미 안전 응답 (refuse) 을 하는데 또 필요한가요?

Claude 의 refuse 는 **모델 출력 수준** 의 가드. Aegis 는 **모델 출력 이후, 실제 tool 실행 직전** 에 또 한 번 검증합니다. PitchDeck 의 *"below the model"* 포지셔닝 — 모델이 의도하지 않은 위험 실행을 잡습니다 (예: 정상 prompt 이지만 컨텍스트 손상으로 destructive args 생성).

### Q4. 라이선스가 만료되면?

Pro+ 기능 (advisor, sLLM judge) 만 비활성되고 **Solo Free 동작은 그대로 유지**. 기존 audit log 도 영원히 본인 소유 — 외부에 송신 안 됨.

### Q5. 기존 OpenAI / Anthropic / vLLM 환경과 충돌하나요?

없음. Aegis 는 tool call layer 에만 끼어들고, LLM 호출 자체는 안 건드립니다. **OpenRouter, Claude, GPT, Gemini, vLLM, Ollama 모두 동시 사용 가능** — 오히려 `--by-provider` cross-grouping 으로 *어느 LLM 이 더 위험한지* 정량 비교 가능.

### Q6. 감사 로그가 진짜 위조 불가능한가요?

네. PitchDeck 의 Crypto-Sign 부분:

1. 매 레코드가 SHA3 hash 로 이전 레코드에 연결 (chain-of-custody)
2. 매 레코드가 Ed25519 로 서명 — Aegis 의 비밀키 없이는 위조 불가
3. `aegis verify-audit` 한 명령으로 외부 검증 가능 — 1 초 이내

수정 / 삭제 시도 시 `verify-audit` 가 즉시 실패. SOC 2 / HIPAA / EU AI Act 가 요구하는 *tamper-evident* 요건 충족.

### Q7. Open Source 인가요?

**Solo Free** = Apache-2.0, 영구 무료. 코드 전체 GitHub 공개.
**Pro / Team / Enterprise** = 같은 코드, 라이선스 키로만 추가 기능 (advisor, sLLM, sidecar) 활성. PitchDeck 의 "no rugpull-bait" 약속.

---

## 11. 자주 발생하는 문제 + 해결

| 증상 | 원인 / 해결 |
|---|---|
| `aegis install` 후 Claude Code 가 후크 무시 | Claude Code **재시작** 필수. 그래도 안 되면 `aegis status` 로 settings.json 패치 상태 확인 |
| `aegis verify-audit` 실패 | 누군가 ~/.aegis/audit.jsonl 을 수정했을 가능성. `aegis forensic last` 로 마지막 정상 레코드 확인 → 해당 시점 이후 backup 으로 복원 |
| `BLOCK` 메시지가 너무 많이 뜸 | Coach baseline 학습이 부족. 1주일 정도 `AEGIS_BURNIN_SHADOW=1` 운영 후 `aegis coach burnin train-m13` |
| `REQUIRE_APPROVAL` 이 너무 자주 뜸 | Autonomy 활성화 검토. `aegis autonomy learn --since 7d` → `aegis autonomy show -v` 후 hook 에 `AEGIS_AUTONOMY_ENABLED=1` 추가 (§6.5) |
| Autonomy 활성화 후에도 같은 패턴이 자동 승인 안 됨 | `aegis autonomy explain <trace_id>` 로 어느 게이트에서 거부됐는지 확인 — 6 안전망 (reversibility / drift / centroid / andon / session-prior / ε-greedy) 중 하나가 fail 한 것 |
| 연속 자동 승인 후 갑자기 사람에게 떨어짐 | Andon tripwire 발동 (기본 20 회). `AEGIS_AUTONOMY_ANDON_THRESHOLD` 로 조정 또는 0 으로 비활성화 |
| `aegis knowledge search` 가 0 hits | wiki 가 비어 있음. `aegis knowledge build --since 30d` 먼저 실행 |
| sLLM advisor 가 wiki 컨텍스트 사용 안 함 | `export AEGIS_ADVISOR_USE_KNOWLEDGE=1` 후 advisor 호출. `aegis knowledge measure <aid>` 로 토큰 delta 확인 |
| Pro 라이선스 활성화 후에도 dummy judge 사용 | 환경 변수 확인: `echo $AEGIS_JUDGE_PROVIDER`. `unset` 또는 `local-phi` / `haiku` 로 설정 |
| OpenRouter route 가 `(no-provider)` 로 잡힘 | `aegis.integrations.openrouter.canonical_provider()` 헬퍼로 provider 문자열 생성하여 ATV header 에 stamp 필요 |
| 한국어 메시지 깨짐 (Windows) | 터미널 UTF-8 설정: `chcp 65001` |

깊은 진단은: [`docs/manuals/DOCTOR_MANUAL.ko.md`](manuals/DOCTOR_MANUAL.ko.md) §5 "trouble shooting"

---

## 12. 다음 단계

### 사용자 수준별 다음 행동

| 당신의 상태 | 다음 한 단계 |
|---|---|
| 막 설치한 Solo Free 사용자 | 일주일 사용 후 `aegis report --since 7d` — 본인 사용 패턴 살펴보기 |
| 일주일 사용 후 승인 피로 누적 | §6.5 Autonomy 활성화 — `aegis autonomy learn --since 7d` 후 hook 에 `AEGIS_AUTONOMY_ENABLED=1` |
| 운영 30일 후 sLLM advice 정밀도 끌어올리기 | §7-2 LLM-Wiki 빌드 — `aegis knowledge build --since 30d` + `export AEGIS_ADVISOR_USE_KNOWLEDGE=1` |
| Solo Free 인데 advisor 기능 궁금 | Pro 시범 — [`PRICING.md`](../PRICING.md) 의 design partner 안내 |
| 기업 / 멀티 사용자 도입 검토 | [`docs/DESIGN_PARTNER_PROGRAM.md`](DESIGN_PARTNER_PROGRAM.md) — 30일 무료 pilot |
| 다른 agent 프레임워크 통합 | [`docs/integrations/`](integrations/) — OpenClaw / OpenRouter / Hermes / Paperclip 각각 |
| 깊은 기술 문서 | [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) — STRIDE walk + auditor checklist |

### 영상 / 데모 자료

- 빠른 시연 GIF: [`demo/recording/quickstart.gif`](../demo/recording/quickstart.gif)
- 1 페이지 데모 시나리오: [`demo/`](../demo/)
- NVIDIA Inception PitchDeck: 본 가이드 작성 시 참조한 자료 (외부 자산)

### 커뮤니티

- GitHub: https://github.com/happyikas/Aegis-ATV
- 사이트: https://aegisdata.ai
- 라이선스 / 영업 문의: `datamonster@aegisdata.ai`

---

## 13. 한 문장 요약 — 다시

> **Aegis 는 당신의 AI 에이전트가 만드는 모든 행동을, 실행 직전에 검증하고, 영원히 위조 불가능한 기록으로 남기는 도구입니다.** 무료로 시작하고, 노트북 밖으로 데이터가 나가지 않으며, 한 명령으로 감사 가능합니다.

설치는 한 줄:

```bash
uv run aegis install --mode local
```

추가 질문이 있으면 위 §10 FAQ 또는 [`docs/manuals/`](manuals/) 의 기능별 매뉴얼을 참조하세요.
