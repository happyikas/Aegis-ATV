# Aegis — 한글판 사용자 매뉴얼 v0.1.0

**Action Firewall for Claude Code · Personal MVP**

> 16-step ATV-2080-v1 firewall · Solo Free 컨트랙트 (0 클라우드 호출) · Apache-2.0
>
> [github.com/happyikas/Aegis-ATV](https://github.com/happyikas/Aegis-ATV)

---

## 목차

- [1장. 개요](#1장-개요)
- [2장. 빠른 시작 (5분)](#2장-빠른-시작-5분)
- [3장. 16-step Firewall 파이프라인](#3장-16-step-firewall-파이프라인)
- [4장. ATV-2080-v1 벡터](#4장-atv-2080-v1-벡터)
- [5장. 지능 티어 — `--profile {free, pro, cloud}`](#5장-지능-티어----profile-free-pro-cloud)
- [6장. CLI 레퍼런스](#6장-cli-레퍼런스)
- [7장. Audit log 와 암호서명 체인](#7장-audit-log-와-암호서명-체인)
- [8장. Cost / Performance / Security Advisor](#8장-cost--performance--security-advisor)
- [9장. 트러블슈팅](#9장-트러블슈팅)
- [10장. 위협 모델 + 보안](#10장-위협-모델--보안)
- [부록 A. 환경 변수 카탈로그](#부록-a-환경-변수-카탈로그)
- [부록 B. 라이선스 + 특허 참조](#부록-b-라이선스--특허-참조)
- [부록 C. 외부 링크 + 변경 이력](#부록-c-외부-링크--변경-이력)

---

## 1장. 개요

### 1.1 Aegis 는 무엇인가

Aegis 는 Claude Code 의 매 도구 호출 (Bash / Read / Edit / Write 등) 을 **PreToolUse 시점에 가로채서**, 16-step ATV-2080-v1 firewall 을 통과시킨 뒤 `ALLOW` · `BLOCK` · `REQUIRE_APPROVAL` 중 하나의 결정을 내리는 in-process 보안 도구입니다.

기존의 "are you sure?" 형 승인 프롬프트는 LLM 이 이미 *"실행해도 괜찮다"* 고 판단한 후의 사후처리입니다. Aegis 는 그 결정 자체를 **사용자 디바이스 안의 deterministic firewall** 로 옮깁니다.

### 1.2 누구를 위한 도구인가

- macOS / Linux 환경에서 Claude Code 를 일상적으로 사용하는 **개인 개발자** — Personal MVP 의 일차 대상.
- Sidecar 모드 (FastAPI 서비스) 를 통한 멀티 테넌트 / 엔터프라이즈 배포는 같은 코드베이스에서 지원하지만 본 매뉴얼의 범위 밖.
- Windows / WSL2 는 v0.1.0 시점에 미지원 — POSIX 전용 `fcntl` 의존성 때문. 추후 지원 예정.

### 1.3 Solo Free 컨트랙트

기본 install (`aegis install --mode local --profile free`) 으로는 **0 외부 네트워크 호출** 이 보장됩니다. firewall, sLLM judge, embedding, audit chain — 모두 사용자 디바이스 안의 in-process Python 코드.

외부 호출이 발생하는 유일한 경우 (모두 명시적 opt-in):

- `AEGIS_JUDGE_PROVIDER=haiku` + `ANTHROPIC_API_KEY` → Anthropic 호출
- `AEGIS_EMBEDDING_PROVIDER=openai` + `OPENAI_API_KEY` → OpenAI 호출
- `aegis pull-model` — 한 번의 GGUF 다운로드, 그 후 로컬

`tcpdump` / Little Snitch 로 검증 가능 — 기본 install 에서는 outbound 트래픽 차트가 평탄.

---

## 2장. 빠른 시작 (5분)

### 2.1 사전 준비

- macOS 또는 Linux (Apple Silicon / Intel / x86_64 모두 지원)
- `claude` CLI 설치 + 정상 동작 (`claude.ai/code` 에서 확인)
- Python 3.11 이상 — `uv` 가 필요시 자동 설치
- 디스크: free profile 약 50 MB, pro/cloud 는 +700 MB GGUF 다운로드

### 2.2 설치 — 세 가지 경로

셋 중 어느 것을 선택해도 동일한 in-process 후크와 동일한 정책으로 끝납니다.

#### (A) 소스 클론 (개발자 모드)

```bash
git clone https://github.com/happyikas/Aegis-ATV.git
cd Aegis-ATV
uv sync                              # 의존성 설치 (~30s)
uv run aegis install --mode local    # ~/.claude/settings.json 패치
```

#### (B) `curl` 원라이너

```bash
curl -LsSf https://raw.githubusercontent.com/happyikas/Aegis-ATV/main/scripts/install.sh | bash
```

스크립트가 자동으로 (1) `uv` 가 없으면 설치, (2) 소스를 `~/.aegis-src` 에 클론, (3) `uv sync` 실행, (4) `aegis install --mode local` 호출. 권한상승 사용자 거부 + 재실행 시 idempotent.

#### (C) Homebrew tap

```bash
brew tap happyikas/aegis https://github.com/happyikas/Aegis-ATV.git
brew install happyikas/aegis/aegis
aegis install --mode local
```

### 2.3 Claude Code 재시작

후크는 Claude Code 가 시작될 때 `~/.claude/settings.json` 에서 한 번 읽힙니다. 설치 후 반드시 Claude Code 를 **완전히 종료 + 재시작** 해야 합니다 (탭 재열기로는 부족).

### 2.4 첫 BLOCK 보기 — 30초

Claude Code 를 새 터미널에서 켜고:

```
> /var/data 디렉터리를 재귀적으로 삭제해줘
```

기대 결과 (즉시):

```
⛔ BLOCK  Bash  trace=ebf0c92d  (165 ms)
  reason: dangerous pattern: <step310 regex>
  advise:
    [HIGH] security-reviewer — Block until human reviewer ACKs
```

도구는 실행되지 않습니다. Claude 에게는 "BLOCKED" 신호만 전달되고, 동시에 `~/.aegis/audit.jsonl` 에 서명된 한 줄이 추가됩니다.

### 2.5 다른 차단 시연

(모두 paraphrase — 실제 명령어 또는 자연어로 시도)

| 시도 | 차단 룰 |
|------|---------|
| force-push 를 protected branch (main / master / production) 에 | `rule:git_destructive` |
| Kubernetes `delete` 명령 (namespace / deployment / pod) | `rule:cloud_destructive` |
| Terraform `destroy` 또는 `apply -auto-approve` | `rule:cloud_destructive` |
| AWS IAM `delete-policy` / `delete-user` | `rule:cloud_destructive` |
| AWS EC2 `terminate-instances` | `rule:cloud_destructive` |
| Helm `uninstall` / `delete` | `rule:cloud_destructive` |
| AWS S3 bucket recursive 강제 제거 | `rule:cloud_destructive` |
| AWS 인증서 파일 / SSH 개인키 read | `rule:sensitive_path_block` |
| SQL drop-table on production | `rule:sql_unbounded` |
| `WHERE` 없는 SQL `delete` | `rule:sql_unbounded` |
| 권한 상승 prefix 명령어 | `rule:privilege_prefix` |
| 동적 코드 실행 함수 호출 (eval / exec / system) | `rule:eval_exec` |

v0.1.0 시점 **31 종 룰 + 6 종 incident playbook** 활성. 자세한 카탈로그: [`policies/rag_corpus/rules.jsonl`](../policies/rag_corpus/rules.jsonl)

---

## 3장. 16-step Firewall 파이프라인

매 PreToolUse 시점에 다음 16개 step 이 순차 실행됩니다. `BLOCK` / `REQUIRE_APPROVAL` 이 한 번 발화하면 그 시점에 short-circuit — 후속 step 은 실행되지 않습니다.

| Step | 이름 | 역할 + 예시 |
|------|------|-------------|
| 305 | safe-allowlist fast-path | `ls` / `git status` / `pytest` 등 known-safe → sLLM skip, < 5 ms |
| 308 | agent identity | v4.2 — agent identity verification (Claim 56) |
| 309 | instruction drift | `CLAUDE.md` / `AGENTS.md` / `.mcp.json` hash baseline 비교 |
| 310 | dangerous-pattern args | filesystem 재귀 삭제 / DB 테이블 drop / 시스템 password path / 권한상승 prefix / 동적 실행 함수 등 |
| 311 | donor rules (cloud destructive) | k8s `delete` / Terraform `destroy` / Helm uninstall / AWS IAM 변경 / git force-push to protected branch 등 |
| 312 | normalize tool args | 도구 인자 canonicalize — 공백 / 인코딩 변형으로 룰 우회 방지 |
| 315 | AID auth | M14 — per-agent-instance 인증 + 회로차단기 (3회 위반 → quarantine) |
| 320 | blast radius | 파일 / 네트워크 / cloud reach 정량화 (1–10 점) |
| 330 | human / role gating | 선언된 role 의 verb 권한 검사 |
| 335 | cost gate | `AEGIS_TOKEN_BUDGET` 초과 → `REQUIRE_APPROVAL` |
| 336 | loop detector | 같은 (tool, args) 3회 → `REQUIRE_APPROVAL`; read-only 반복은 dedupe |
| 337 | HW band anomaly | T3 hardware-emulation 갈래 — T2 에서는 no-op |
| 340 | policy + sLLM judge | `policies/default.json` 의 deny/allow → 회색 case 는 sLLM judge |
| 350 | approval gate | `REQUIRE_APPROVAL` 결정의 사용자 ACK 대기 |
| 360 | audit chain | 결정을 `~/.aegis/audit.jsonl` 에 SHA3-chained / Ed25519 signed (opt-in) 으로 append |
| 370 | exec or BLOCK | `ALLOW` → 도구 실행; `BLOCK` / 미승인 RA → Claude 에게 거부 신호 반환 |

각 step 의 trace 는 audit log 의 `explain.step_traces` 필드에 한 줄씩 보존됩니다. `AEGIS_STEP_TIMING_ENABLED=1` 을 설정하면 step 별 microsecond latency 가 `explain.step_timings_us` 에도 함께 stamp 됩니다.

---

## 4장. ATV-2080-v1 벡터

매 도구 호출은 30개의 named subfield × 합 2,080 float32 벡터로 인코딩됩니다. 16-step firewall, sLLM judge, loop detector, RAG corpus, M13 attribution head — 모두 **같은 벡터를 각자의 관점에서** 읽습니다.

### 4.1 SW 밴드 (0..1879, 19 subfield)

| 인덱스 | Subfield | 용도 |
|--------|----------|------|
| 0..767 | `agent_state_embedding` (768) | 에이전트 상태 임베딩 — RAG retrieval / case memory |
| 768..1407 | `action_history` (640) | 최근 N 호출의 (tool, args) 시퀀스 인코딩 |
| 1408..1535 | `inter_agent_graph` (128) | 다중 에이전트 협업 그래프 (sidecar) |
| 1536..1599 | `memory_provenance` (64) | 도구 인자가 어느 상위 결정에서 유래했는지 |
| 1600..1615 | `qom_scores` (16) | Quality-of-Memory — 컨텍스트 신선도 / 일관성 |
| 1616..1647 | `resource_access_pattern` (32) | 파일 / 네트워크 / DB 접근 패턴 시그니처 |
| 1648..1663 | `prompt_structure` (16) | 16개 스칼라 — 길이 / 깊이 / 인용비율 |
| 1664..1671 | `aid_ats_scalars` (8) | agent_id / atv_session_id 변환 스칼라 |
| 1672..1683 | `encryption_metadata` (12) | tenant scope + key version |
| 1684..1747 | `output_content_fingerprint` (64) | 예상 출력 SHA3 fingerprint |
| 1748..1779 | `tool_arg_inspection` (32) | 정규표현식 hit + 위험 키워드 score |
| 1780..1795 | `action_blast_radius` (16) | step320 의 정량화된 영향 범위 |
| 1796..1807 | `output_channel_diversity` (12) | stdout / file / network 등 출력 다양성 |
| 1808..1823 | `session_behavioral_drift` (16) | 세션 평균 대비 이상치 percentile |
| 1824..1835 | `mcp_trust_signals` (12) | MCP 서버 trust + 기원 hash |
| 1836..1851 | `grounding_metrics` (16) | evidence-citation ratio |
| 1852..1855 | `novelty_score` (4) | 이번 세션에서 본 적 없는 패턴 점수 |
| 1856..1863 | `human_oversight_state` (8) | 최근 사용자 승인 빈도 / 패턴 |
| 1864..1879 | `cost_efficiency_metrics` (16) | `cumulative_dollars` / `forecasted_cost` / `burn_rate` |

### 4.2 HW 밴드 (1880..2079, 11 subfield, 200-D)

T2 (소프트웨어 MVP) 에서는 0 으로 채워집니다. 특허 ¶[0042] 의 T3 hardware emulation 분기에서만 `step337_hw_anomaly` 가 활성화. Personal MVP 사용자는 이 밴드를 직접 다룰 일이 없습니다.

---

## 5장. 지능 티어 — `--profile {free, pro, cloud}`

v0.1.0 (PR-A) 부터 `aegis install` 이 세 가지 사전 설정을 제공합니다. 모델 자동 다운로드 + advisor 활성화 + judge / embedding 선택을 한 번에 처리.

| Profile | Embedding | Judge | Advisor | 디스크 | 외부 호출 |
|---------|-----------|-------|---------|--------|-----------|
| **free** (기본) | dummy (SHA3) | dummy (룰) | OFF | ~50 MB | 0 |
| **pro** | bge-local | hybrid (M13+local-phi) | heuristic | +700 MB GGUF | 0 |
| **cloud** | bge-local | hybrid + Haiku 회색지대 | haiku | +700 MB GGUF | `ANTHROPIC_API_KEY` 필요 |

사용 예:

```bash
# Personal MVP 의 기본 — 0 클라우드 0 모델
uv run aegis install --mode local --profile free

# 본격 사용 — 진짜 semantic retrieval + M13 attribution head
uv run aegis install --mode local --profile pro
# → 자동으로 bge-base-en (~100MB) + llama-3.2-1b (~700MB) 다운로드
# → 이미 받은 파일은 fast-path skip (idempotent)

# 정확도 우선 — 회색지대 호출만 클라우드 escalate
export ANTHROPIC_API_KEY=sk-ant-...
uv run aegis install --mode local --profile cloud
```

explicit `--judge` / `--embedding` 가 profile baseline 을 덮어씁니다 — `--profile pro --judge dummy` 는 pro 의 advisor + bge-local 은 유지하되 judge 만 dummy 로 고정.

- **free** — 다운로드 없음
- **pro / cloud** — `bge-base-en` (~100 MB) + `llama-3.2-1b` (~700 MB)
- 둘 다 idempotent — 이미 있는 파일은 fast-path
- 다운로드 실패 시 install 이 `settings.json` 을 patch 하지 않고 거부 (절반 설정 상태 방지)

---

## 6장. CLI 레퍼런스

### `aegis install`

`~/.claude/settings.json` 의 PreToolUse 후크를 등록.

- `--mode {sidecar,local}` — 기본 sidecar; Personal MVP 는 local
- `--profile {free,pro,cloud}` — 지능 티어 사전설정 (5장 참조)
- `--judge {dummy,hybrid,local-phi}` — 명시 시 profile baseline 덮어씀
- `--embedding {dummy,bge-local}` — 동일
- `--force` — 기존 후크 위에 강제 재설치
- `--rescue` — `settings.json.bak.<latest>` 자동 복원

```bash
uv run aegis install --mode local --profile pro
```

### `aegis uninstall`

Aegis 가 설치한 후크만 제거 (사용자 자체 후크 보존).

- `--dry-run` — 무엇이 제거될지만 출력
- `--no-backup` — `settings.json.bak.<ts>` 생략

```bash
uv run aegis uninstall --dry-run
```

### `aegis report`

최근 세션의 5줄 risk 요약.

- `--audit PATH` — 다른 audit 로그 파일 지정
- `--since DURATION` — `7d` / `24h` / `3600` 등
- `--explain TRACE_OR_LAST` — 한 결정의 layer-by-layer 설명
- `--json` — 구조화 JSON

```bash
uv run aegis report --since 24h
```

### `aegis verify-audit`

Audit chain 무결성 검증 (SHA3 + 옵션 Ed25519).

```bash
uv run aegis verify-audit
```

### `aegis audit-key`

Ed25519 서명 키 관리 (opt-in).

- `init` — `~/.aegis/keys/audit.ed25519{,.pub}` 생성
- `show` — 공개 fingerprint 와 경로 출력

```bash
uv run aegis audit-key init
```

### `aegis forensic <selector>`

단일 세션의 시간순 timeline (PR-C).

- `<selector>` — AID 또는 `last` / `LAST` / trace prefix
- `--trace TRACE_ID` — 한 호출만 narrow
- `--since DURATION` — 시간 창
- `--limit N` — 최근 N 건만
- `--json` — 구조화 출력

```bash
uv run aegis forensic last --limit 20
```

### `aegis advise [selector]`

Live cost / performance / security advisor 추천.

- `[selector]` — `last` (기본) / AID / `all`
- `--category {cost,performance,security,all}`
- `--priority {high,medium,low,all}`
- `--since DURATION` — 기본 7d
- `--json`

```bash
uv run aegis advise --category cost
```

### `aegis pull-model`

Local sLLM GGUF 다운로드.

- `--model NAME` — `bge-base-en` / `llama-3.2-1b` / `phi3-mini` 등
- `--list` — 사용 가능 모델 목록
- `--recommend` — 디바이스 별 추천
- `--force` — 이미 있어도 재다운로드

```bash
uv run aegis pull-model --recommend
```

### `aegis policy`

정책 / 룰 변경 이력 + 본문 조회.

- `diff --since DURATION` — 시간창 내 추가 / 만료 룰
- `log` — 룰 모듈 commit history
- `show RULE_ID` — 룰 본문 + 예시

```bash
uv run aegis policy diff --since 7d
```

### `aegis baseline`

step309 instruction-drift baseline 관리.

- `reattest` — `CLAUDE.md` / `AGENTS.md` / `.mcp.json` 등 재해시
- `show` — 현재 baseline 상태

```bash
uv run aegis baseline reattest
```

### `aegis cost`

비용 집계 + replay.

- `summary` — 시간창별 누적 / 위험 호출
- `replay <transcript>` — 다른 모델로 비용 시뮬레이션

```bash
uv run aegis cost summary --since 7d
```

### `aegis budget`

테넌트별 예산 관리 (sidecar).

- `show` — 현재 예산 + 잔액
- `set --daily X` — daily ceiling 변경

```bash
uv run aegis budget show
```

---

## 7장. Audit log 와 암호서명 체인

### 7.1 위치

| 경로 | 용도 |
|------|------|
| `~/.aegis/audit.jsonl` | 메인 결정 로그 (append-only, JSONL, SHA3-체인) |
| `~/.aegis/audit.jsonl.1`, `.2`, … | rotation 된 옛 파일 |
| `~/.aegis/keys/audit.ed25519` | Ed25519 개인키 (opt-in, mode 0600) |
| `~/.aegis/keys/audit.ed25519.pub` | Ed25519 공개키 |
| `~/.aegis/intent_log.sqlite` | ATMU 2PC intent 로그 (M10) |
| `~/.aegis/budgets.sqlite` | per-tenant cost ledger (M12) |

### 7.2 두 단계 무결성

**단계 1 — SHA3-256 체인** (항상 활성, 키 불필요):

- 각 record 에 `prev_hash` + `this_hash` 추가
- 한 줄 변조 시 그 이후 모든 hash 가 깨짐
- post-write mutation 검출 — 단, 전체 체인 재계산은 가능

**단계 2 — Ed25519 서명** (opt-in):

```bash
uv run aegis audit-key init    # 한 번만
# 그 이후 모든 audit append 가 자동 서명
uv run aegis verify-audit       # signed 표시 확인
```

- 서명은 `this_hash` 위에 — chain integrity 와 결합
- 전체 체인 재계산이 private key 없이는 불가
- 공유 audit / 규제 / 공증 용도에 권장

### 7.3 Concurrency 안전성

v0.1.0 (PR #109) 부터 audit append 는 `fcntl.flock(LOCK_EX)` 으로 직렬화됩니다 — 다중 Claude Code 세션이 동시에 같은 `audit.jsonl` 에 write 해도 chain fork 가 발생하지 않습니다.

### 7.4 Audit log archive

주기적 archive 패턴 (90일 이전 분리):

```python
import json, time
cutoff = time.time_ns() - 90 * 86400 * 10**9
with open('~/.aegis/audit.jsonl') as f, \
     open('~/.aegis/audit.archive.jsonl', 'a') as out:
    for line in f:
        if json.loads(line).get('ts_ns', 0) < cutoff:
            out.write(line)
```

또는 `AEGIS_AUDIT_MAX_BYTES` 환경변수로 자동 회전.

---

## 8장. Cost / Performance / Security Advisor

`--profile pro` / `cloud` 활성화 시, 같은 ATV 위에서 세 advisor 가 각자 관점으로 추천을 생성합니다. 결과는 audit 의 `explain.action_advice` 에 저장되고, `aegis advise` 로 surface 가능.

### 8.1 8-advisor 카탈로그

| 카테고리 | Advisor | 발화 신호 |
|----------|---------|-----------|
| Cost | `cost-optimizer` | 비용 burn 1.5× / budget 임박 |
| Cost | `kv-cache-optimizer` | cache hit rate 급락 / prefix re-key |
| Cost | `context-compactor` | 토큰 속도 / context saturation |
| Performance | `test-runner` | 에러 패턴 검출 |
| Performance | `loop-breaker` | 같은 호출 ≥3회 반복 |
| Performance | `human-clarifier` | backtrack / 혼란 신호 |
| Security | `security-reviewer` | destructive path / privilege |
| Security | `permission-escalator` | 회색지대 ambiguous op |

### 8.2 사용 예

```bash
# 최근 세션의 모든 advisor 추천
uv run aegis advise

# Cost 카테고리만
uv run aegis advise --category cost

# 최근 24h, HIGH 우선순위만
uv run aegis advise --since 24h --priority high

# JSON 으로 추출
uv run aegis advise --json | jq '.recommendations[] | select(.count > 5)'
```

### 8.3 출력 예 (실제 dogfooding 결과)

```
aegis advise — 2 recommendation(s) from last 7d
  audit:               ~/.aegis/audit.jsonl
  records walked:      454
  advisor invocations: 217

  🔴 HIGH    [COST       ]  kv-cache-optimizer ×4
             "Audit recent prompt-prefix mutations; cache is
              being re-keyed on most turns."
             Why: cache_hit_rate_max_drop_pp=95pp; prefix re-keys=1
             Steps:
               · prune-turns turns=[0] saves $0.00

  🟡 MEDIUM  [SECURITY   ]  permission-escalator ×213
             "Surface verdict to operator before proceeding."
```

---

## 9장. 트러블슈팅

### 9.1 후크가 호출되지 않는다

```bash
# 1) 후크 등록 확인
cat ~/.claude/settings.json | grep -A1 PreToolUse

# 2) 실행 권한 확인
ls -l tools/aegis_local_hook.py

# 3) Python venv 살아있는지
uv run python -c 'import aegis'

# 4) Claude Code 를 완전히 종료 후 재시작 했는지 (탭 재열기로 부족)
```

### 9.2 매 호출이 너무 느림

```bash
# 1) 어떤 judge 가 활성인지
echo $AEGIS_JUDGE_PROVIDER

# 2) dummy 가 가장 빠름 — free profile 로 회귀
uv run aegis install --mode local --profile free --force

# 3) step 별 latency 분해 (PR-D)
export AEGIS_STEP_TIMING_ENABLED=1
tail -1 ~/.aegis/audit.jsonl | python3 -c "
import json, sys
r = json.loads(sys.stdin.read())
ts = r.get('explain', {}).get('step_timings_us', {})
for k, v in sorted(ts.items(), key=lambda x: -x[1]):
    print(f'{v:>6} us  {k}')
"
```

### 9.3 차단 안 되어야 할 것이 차단됨

```bash
# 1) 어떤 룰이 fired 했는지 audit 에서 확인
tail -1 ~/.aegis/audit.jsonl | python3 -m json.tool | grep reason

# 2) 해당 룰을 safe-allowlist 에 추가
#    (policies/safe_actions.json)

# 3) Claude Code 재시작
```

### 9.4 비상 복구 — `aegis install --rescue`

잘못된 옵션으로 install 해서 후크가 본인을 막는 경우 (cost gate self-DoS, 누락된 환경변수 등) — 별도 터미널에서 (Aegis-필터링 안 되는 셸):

```bash
uv run aegis install --rescue
# → ~/.claude/settings.json.bak.<latest> 자동 복원
```

v0.1.0 부터 Personal `--mode local` install 은 anti-self-DoS default 가 자동 prepend (`AEGIS_APPROVE_AS_BLOCK=0` + `AEGIS_TOKEN_BUDGET=99999999`). 이 상황은 더 이상 발생하지 않아야 정상.

### 9.5 Audit log 가 너무 큼

`~/.aegis/audit.jsonl` 은 append-only. `AEGIS_AUDIT_MAX_BYTES` (rotation threshold) 환경변수로 자동 회전. 또는 7.4 의 archive 패턴 참조.

---

## 10장. 위협 모델 + 보안

### 10.1 위협 모델 가정

**가정 (in scope)**:

- 로컬 사용자는 신뢰됨 — 키를 가지므로 무엇이든 위조 가능
- LLM 은 정직하지만 오류 가능 — 추론이 destructive shortcut 으로 이끌릴 수 있음
- LLM 의 input (도구 인자, 에이전트 상태) 은 부분적으로 poisoned 가능 — 변조된 `CLAUDE.md`, 주입된 `.mcp.json`, downstream document 의 prompt-injection

**가정하지 않음 (out of scope)**:

- 로컬 사용자가 공격자 (그런 경우 sidecar 멀티 테넌트가 정답)
- OS-level 샌드박싱이 부재 (Aegis 는 그 위에 layer)
- 임의 자연어의 prompt-injection 검출 (step309 는 named files 의 drift 만)

### 10.2 보안 취약점 신고

**공개 GitHub issue 로 보안 취약점을 신고하지 마세요.** [SECURITY.md](../SECURITY.md) 에 기재된 메인테이너 이메일로 `[aegis-security]` 프리픽스로 발송. 확인 48시간 / triage 7일 / 고심도 issue 30일 fix 목표.

### 10.3 In scope 취약점

| 영역 | 예시 |
|------|------|
| Firewall bypass | rule 회피하는 인코딩 / 유니코드 / 공백 트릭 |
| Audit chain forgery | `verify-audit` 가 통과하면서도 record 변조 / 재배열 |
| Key extraction | private key 가 디스크에서 읽히거나 로그에 노출 |
| Sandbox escape | 후크 프로세스의 권한 상승 / argv 인젝션 |
| Self-DoS | 정상 호출이 영구 wedge 되는 pathological 입력 |
| Sensitive-data leak | local mode 가 의도치 않은 클라우드 호출 / audit 에 secret 포함 |

---

## 부록 A. 환경 변수 카탈로그

| 환경 변수 | 기본값 | 효과 |
|-----------|--------|------|
| `AEGIS_TENANT_ID` | `claude-code-local` | audit record 의 tenant 태그 |
| `AEGIS_LOCAL_AUDIT` | `~/.aegis/audit.jsonl` | audit 로그 경로 |
| `AEGIS_APPROVE_AS_BLOCK` | `0` (local) / `1` (sidecar) | `REQUIRE_APPROVAL` → `BLOCK` 격상 |
| `AEGIS_HOOK_VERBOSE` | `0` | `1` → ALLOW 도 stderr 출력 |
| `AEGIS_POLICY_DIR` | `./policies` | `sensitive_paths.json` 위치 |
| `AEGIS_TOKEN_BUDGET` | `1.0` / `99999999` (local) | step335 cost gate ceiling (USD) |
| `AEGIS_EMBEDDING_PROVIDER` | `dummy` | `dummy` / `openai` / `bge-local` |
| `AEGIS_JUDGE_PROVIDER` | `dummy` | `dummy` / `hybrid` / `local-phi` / `haiku` |
| `AEGIS_JUDGE_MODEL_PATH` | `(none)` | local-phi GGUF 경로 |
| `AEGIS_EMBEDDING_MODEL_PATH` | `(none)` | bge-local GGUF 경로 |
| `AEGIS_ADVISOR_ENABLED` | `0` | `1` → cost/perf/sec advisor 활성 |
| `AEGIS_ADVISOR_PROVIDER` | `(none)` | `heuristic` / `haiku` |
| `AEGIS_STEP_TIMING_ENABLED` | `0` | `1` → 매 step μs 를 audit 에 stamp |
| `AEGIS_INSTRUCTION_BASELINE_PATH` | `(empty)` | step309 baseline 파일 |
| `AEGIS_AUDIT_SIGNING_KEY` | `~/.aegis/keys/audit.ed25519` | Ed25519 개인키 경로 |
| `AEGIS_AUDIT_PUBKEY` | `~/.aegis/keys/audit.ed25519.pub` | Ed25519 공개키 경로 |
| `AEGIS_AUDIT_MAX_BYTES` | `0` | `audit.jsonl` 회전 threshold |
| `AEGIS_AUDIT_MAX_ROTATIONS` | `(none)` | 보존할 회전 파일 수 |
| `AEGIS_INTENT_LOG_DB` | `~/.aegis/intent_log.sqlite` | ATMU 2PC intent 로그 |
| `AEGIS_ATMU_DISABLE` | `0` | `1` → ATMU 비활성 (read-only fs) |
| `AEGIS_BURNIN_SHADOW` | `0` | `1` → M13 retraining 용 shadow |
| `AEGIS_HW_PROVIDER` | `none` | `sim` 시 step337 활성 |
| `ANTHROPIC_API_KEY` | `(none)` | haiku judge / advisor (cloud profile) |
| `OPENAI_API_KEY` | `(none)` | openai embedding |

---

## 부록 B. 라이선스 + 특허 참조

### B.1 라이선스

Aegis 는 **Apache License Version 2.0** 으로 배포됩니다. 본문 전체는 프로젝트 루트의 [LICENSE](../LICENSE) 파일 또는 [www.apache.org/licenses/LICENSE-2.0](http://www.apache.org/licenses/LICENSE-2.0) 참조.

- 사용 / 수정 / 배포 자유
- §3 명시적 특허 grant — 기여자가 라이선스 가능한 모든 청구항
- 수정판 배포 시 [NOTICE](../NOTICE) 파일 보존 의무
- 원작자 / 라이선스 표기 의무

### B.2 특허 참조

Aegis 의 아키텍처는 **AegisData patent v4** 의 청구항을 소프트웨어로 구현한 것입니다. 매 milestone 이 어느 청구항을 실현하는지는 프로젝트 루트의 [README.md](../README.md) "What's in the box" 표 참조.

### B.3 보고

| 종류 | 채널 |
|------|------|
| 버그 / 잘못된 룰 / UX 이슈 | GitHub Issues — `bug-report` 템플릿 |
| Firewall bypass / audit forgery / 키 leak | [SECURITY.md](../SECURITY.md) 의 비공개 채널 |
| 검출 룰 / playbook 기여 | GitHub Issues — `detection-rule` 템플릿 |
| 기능 요청 | GitHub Issues — `feature-request` 템플릿 |

---

## 부록 C. 외부 링크 + 변경 이력

### C.1 문서 / 자료

| 자료 | 위치 |
|------|------|
| GitHub repository | [github.com/happyikas/Aegis-ATV](https://github.com/happyikas/Aegis-ATV) |
| v0.1.0 release | [Releases — v0.1.0](https://github.com/happyikas/Aegis-ATV/releases/tag/v0.1.0) |
| 빠른 시작 (한국어) | [docs/PERSONAL_QUICKSTART.md](PERSONAL_QUICKSTART.md) |
| 블로그 포스트 (영어) | [docs/launch/blog_post.md](launch/blog_post.md) |
| FAQ (영어) | [docs/launch/FAQ.md](launch/FAQ.md) |
| Show HN 초안 | [docs/launch/SHOW_HN.md](launch/SHOW_HN.md) |
| dogfooding 실측 자료 | [docs/launch/dogfooding/](launch/dogfooding/) |
| ATV-2080-v1 다이어그램 | [docs/diagrams/atv_2080_v1.png](diagrams/atv_2080_v1.png) |
| v2.2 매뉴얼 (한국어) | [docs/MANUAL_v2.2.md](MANUAL_v2.2.md) |
| macmini 검증 (한국어) | [docs/MANUAL_macmini_validation.md](MANUAL_macmini_validation.md) |

### C.2 v0.1.0 주요 변경

- PR #109 — audit-key UX + concurrent-append 안전성 (`fcntl.flock`)
- PR #110 — Apache-2.0 라이선스 + NOTICE + pyproject 메타데이터
- PR #111 — `aegis install --profile {free,pro,cloud}` + 자동 모델 다운로드
- PR #112 — `AEGIS_STEP_TIMING_ENABLED` 로 step 별 latency 측정
- PR #113 — `aegis forensic <selector>` 단일 세션 timeline
- PR #114 — `aegis advise` 라이브 advisor 추천 surface
- PR #115 — Homebrew Formula sha256 v0.1.0 bump

### C.3 v0.1.0 시점 미포함 (v0.1.1+ 예정)

- GPG-서명 release tarball — 프로젝트 GPG 키 확립 후
- PyPI 배포 — packaging 메타데이터는 준비됨
- Windows / WSL2 native — POSIX 전용 `fcntl` 의존성
- Homebrew-core 등재 — 30일 release history 후
- MCP server 패키징

---

*— 매뉴얼 끝 —*
