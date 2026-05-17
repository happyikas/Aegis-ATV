# Aegis MVP - Claude Code Project Guide

## Project Context
이 프로젝트는 AegisData 특허 v4의 T2 티어 MVP 구현입니다.
핵심 설계는 `PLAN.md`에 있습니다. 변경 전에 반드시 해당 섹션을 읽으세요.

## Architecture
- FastAPI 서비스 (`src/aegis/main.py`)
- 7-step Action Firewall (`src/aegis/firewall/`)
- Claude Haiku 기반 sLLM judge
- Ed25519 + Merkle-chained SQLite 감사 로그

## Commands
- `uv sync` — 의존성 설치
- `uv run pytest` — 테스트
- `uv run ruff check . && uv run mypy src` — 린트·타입
- `uv run uvicorn aegis.main:app --reload` — 개발 서버
- `docker compose up --build` — 전체 스택

## Code Style
- Python 3.11, type hints 필수
- Pydantic v2 모델로 데이터 경계 표현
- async 함수는 FastAPI handler에서만 (내부 로직은 sync)
- 모든 외부 호출(OpenAI, Anthropic)은 retry + timeout 필수
- 로깅은 structlog 사용, `structlog.get_logger()`

## Testing Rules
- 새 step 함수 추가 시 반드시 `tests/unit/test_stepXXX.py` 동반
- 통합 테스트는 Anthropic API를 mock 처리 (pytest fixture / respx)
- 커버리지 70% 유지

## Security Notes
- Ed25519 private key는 `./keys/`에만 존재, 커밋 금지 (.gitignore)
- API 키는 `.env`에서 로드, 절대 하드코딩 금지
- 감사 로그는 append-only — 기존 레코드 수정/삭제 코드 작성 금지

## Where Things Live
- ATV 스키마: `src/aegis/schema.py`
- Firewall: `src/aegis/firewall/step*.py`
- Policies: `policies/*.json`
- Demo: `demo/agent_demo.py`

## Dummy/Mock Mode
외부 API 키 없이도 동작해야 합니다.
- `AEGIS_EMBEDDING_PROVIDER=dummy` → 결정적 SHA3 기반 임베딩
- `AEGIS_JUDGE_PROVIDER=dummy` → 결정적 룰 기반 verdict
이 두 설정을 기본값으로 두고, 실제 키가 .env에 들어오면 openai/haiku로 전환.

## v0.5.11 – 0.5.14 — Autonomy (human-in-the-loop minimizer)

Routine `REQUIRE_APPROVAL` events get auto-bypassed when they match
a learned trust pattern. Every bypass is permanently stamped in the
audit chain (`aegis.autonomy.step331.run` in `step_traces`) so the
operator never loses traceability.

**Trust table**: `~/.aegis/autonomy/trust_table.json` (override via
`AEGIS_AUTONOMY_TRUST_TABLE`). Built by `aegis autonomy learn` —
explicit, batch, replayable. Same discipline as `aegis coach burnin`.

**Bayesian backbone (v0.5.12)** — closes seven ML-training side-
effects: Beta(α, β) posterior with LCB decision rule (overfit),
ε-greedy forced exploration (self-confirming loop), empirical-Bayes
hierarchical prior (spurious correlation), exponential decay
(staleness), Jensen-Shannon drift detection, ternary reward shaping
(CLEAN/+1 · BLOCK/+3β · EXPLICIT_DENY/+10β), 80/20 train/val split
with ECE-gated calibration (miscalibration), Bonferroni-adjusted
min_samples (multiple comparisons).

**Wired (v0.5.13)**: `src/aegis/api/evaluate.py` after `run_firewall`;
`tools/aegis_local_hook.py` after M12 cost-divergence, before
`_atmu_finalize_intent`. Strictly additive — short-circuits when
`AEGIS_AUTONOMY_ENABLED` is unset.

**CLI**:
```
aegis autonomy learn [--since 30d] [--min-trust 0.85] [--credibility 0.95]
                     [--half-life-days 30] [--ece-threshold 0.10] [--force]
aegis autonomy show [-v]
aegis autonomy outliers [--since 7d] [--block-lookahead 10]
aegis autonomy deny <trace_id> [--note "reason"]
```

**Env flags**:
- `AEGIS_AUTONOMY_ENABLED=1` — master switch (default off, byte-identical legacy)
- `AEGIS_AUTONOMY_EPSILON=0.05` — forced exploration rate (clamped to [0.0, 0.5])
- `AEGIS_AUTONOMY_TRUST_TABLE`, `AEGIS_AUTONOMY_DENIALS`, `AEGIS_AUTONOMY_HALF_LIFE_DAYS` — path / behaviour overrides

**v0.5.14 doctor integration**: `aegis doctor --since 7d` includes
🤖 Autonomy section — bypass count, ε-greedy explore count, outlier
table when auto-approvals followed by BLOCK exist. Single-line "no
data" when the operator hasn't opted in.

## v0.5.20 – 0.5.25 — Autonomy & knowledge maturation

### v0.5.20 — wiki event-level kinds
Added three event-level entry kinds beyond the v0.5.15 entity-
level set:

* **SESSION** — gap-segmented activity bursts (30-min split).
* **INCIDENT** — one BLOCK + 3-call setup + 2-call recovery.
* **WORKFLOW** — recurring tool bigrams ≥3 occurrences per agent.

Tunables: `SESSION_GAP_SECONDS`, `SESSION_MIN_CALLS`,
`INCIDENT_BEFORE`, `INCIDENT_AFTER`, `WORKFLOW_MIN_OCCURRENCES`.

### v0.5.21 — TF-IDF semantic search
`aegis knowledge search <query> [-k 10] [--min-score 0.05] [--json]`
runs TF-IDF + cosine ranking over the wiki. Pure-Python, no ML
deps, deterministic, mtime-cached. Tokeniser preserves `:` and
`-` so `loop:Bash` / `rule-fired` stay one token.

### v0.5.22 — reversibility classifier
`policies/reversibility.json` (23 rules) tags every (tool, args)
pair as `trivial / reversible / costly / irreversible`. Hard
gate in `apply_autonomy_bypass`: **irreversible actions are
never auto-bypassed** regardless of trust score, drift, or
ε-greedy. Independent safety floor — separate from the
statistical trust learner.

* CLI: `aegis reversibility check <tool> <args> [--json]`
* Env: `AEGIS_REVERSIBILITY_POLICY` (override bundled file)
* `AEGIS_POLICY_DIR=/path/to/policies` also honoured

### v0.5.23 — ATV centroid bypass (Mahalanobis gate)
Per-pattern centroid + diagonal covariance in 3-D log-feature
space `(log cost, log tokens_in, log latency)`. Collected from
CLEAN records at learn time. Runtime: Mahalanobis-diagonal
distance > 3σ ⇒ refuse bypass. Skipped when
`centroid_n_samples < 20` (sparse patterns fall through to
standard trust-score path).

### v0.5.24 — andon tripwire
Persistent counter at `~/.aegis/autonomy/andon_state.json`
tracks consecutive auto-bypasses across the per-call hook
process. After N (default 20) consecutive bypasses, the next
one is forced to the human. Counter resets when tripwire fires.
Independent of ε-greedy.

* Env: `AEGIS_AUTONOMY_ANDON_THRESHOLD` (0 disables)
* Env: `AEGIS_AUTONOMY_ANDON_STATE` (path override)

### v0.5.25 — session-prior calibration
Operator tags the work session with a risk label that scales
`min_trust`:

* `exploring`   — 0.70 (loose; POC, casual coding)
* `refactor`    — 0.85 (default)
* `prod-deploy` — 0.95 (strict; release work)

State at `~/.aegis/autonomy/session_prior.json` with 8h TTL
auto-expiry. CLI: `aegis autonomy session {start,status,end}`.
Active label is stamped into `step_traces` on bypass for audit.

### Aggregate env-flag inventory (v0.5.11–0.5.25)

| Flag | Purpose | Default |
|---|---|---|
| `AEGIS_AUTONOMY_ENABLED` | master autonomy switch | `0` |
| `AEGIS_AUTONOMY_EPSILON` | ε-greedy forced exploration | `0.05` |
| `AEGIS_AUTONOMY_TRUST_TABLE` | trust table path override | — |
| `AEGIS_AUTONOMY_DENIALS` | explicit-deny log path | — |
| `AEGIS_AUTONOMY_HALF_LIFE_DAYS` | decay τ (Bayesian backbone) | `30` |
| `AEGIS_AUTONOMY_ANDON_THRESHOLD` | andon trip count | `20` |
| `AEGIS_AUTONOMY_ANDON_STATE` | andon counter path | — |
| `AEGIS_AUTONOMY_SESSION_PRIOR` | session-prior state path | — |
| `AEGIS_KNOWLEDGE_DIR` | wiki directory override | — |
| `AEGIS_ADVISOR_USE_KNOWLEDGE` | sLLM advisor wiki injection | `0` |
| `AEGIS_REVERSIBILITY_POLICY` | reversibility rules override | — |

## v0.5.15 – 0.5.18 — ContextMemory knowledge layer (LLM-wiki)

Raw ContextMemory (audit chain) stays. **On top of it** is a derived
wiki-shaped layer the sLLM advisor consumes for workflow advice. Each
entity (agent / tool / pattern) becomes a self-contained article
with infobox + sections + cross-references + tags + confidence.

**Wiki dir**: `~/.aegis/knowledge/` (override via `AEGIS_KNOWLEDGE_DIR`).
One JSON per entry + `index.json` catalog. Built by
`aegis knowledge build`.

**Schema** (`KnowledgeEntry`): `entry_id` (canonical URN like
`agent/foo`, `tool/Bash`, `pattern/loop:Bash`), `summary` (1-2
sentences, always shown first), `infobox` (key-value table — LLMs
parse this most reliably), `sections` (ordered markdown), `related`
(cross-refs), `tags` (semantic filter), `n_observations`,
`confidence`.

**sLLM advisor consumption (v0.5.16 – v0.5.17)**: when
`AEGIS_ADVISOR_USE_KNOWLEDGE=1` and the dispatcher has `aid`, the
wiki block is auto-fetched via `knowledge_context_for_advisor(aid)`
(mtime-keyed cache; never raises) and spliced into the prompt:

- `aegis.judge.advisor.compose_advice_sllm` (production: HaikuAdvisor)
- `aegis.judge.action_advice_sllm.compose_advice`
- `aegis.judge.triple_axis_advisor.assess_via_sllm`

`tools/aegis_local_hook.py` passes `aid=inp.header.aid` to the
dispatcher — the hot-path activation line.

**CLI**:
```
aegis knowledge build [--since 30d] [--out <dir>]
aegis knowledge list [--kind agent|tool|pattern] [--tag X] [--limit N]
aegis knowledge show <entry_id> [--out <file>]
aegis knowledge advisor-context <aid> [--max-related N]
aegis knowledge measure <aid> [--json]      # diagnostic — v0.5.18
```

**Env flags**:
- `AEGIS_KNOWLEDGE_DIR` — wiki directory override
- `AEGIS_ADVISOR_USE_KNOWLEDGE=1` — opt-in for sLLM advisors

**Demo**: `uv run python demo/wiki_grounded_advisor.py` — fully
deterministic, no LLM call, no network. Synthesises 5 agent profiles,
builds the wiki, prints the prompt-size delta (with vs without wiki).
Each wiki-backed agent gets ~600–1,000 extra tokens of structured
background context.

## v2.2 — must-install surface (since 2026-04-27)

v2.1 + v2.2 added five "must-install" features on top of v2.0:

- **step305 safe allowlist** (`policies/safe_actions.json`) → step340
  skips sLLM judge for known-safe ops. Latency <5 ms.
- **step311 cloud destructive** patterns (kubectl / terraform / aws iam
  / gcloud / az / helm / docker) + `sql_unbounded` rule.
- **step336 loop detector** — same call ≥3× → REQUIRE_APPROVAL;
  read-only repeats deduped.
- **step309 instruction drift** — CLAUDE.md / AGENTS.md / .mcp.json /
  plugin & skill manifests baselined; any drift BLOCKs every
  PreToolUse until `aegis baseline reattest`.
- **`aegis report`** — 5-line session risk summary. **`aegis verify-audit`**
  walks the local SHA3 chain.

New runtime config: `AEGIS_INSTRUCTION_BASELINE_PATH` (opt-in for
step309). Default empty → no-op.

## v2.0 — Two Deployment Modes

v2.0.0 부터 같은 코드베이스가 두 가지 배포 모드를 지원합니다 (자세한 내용은
`CHANGELOG.md` v2.0.0 참조):

- **Sidecar 모드** (default) — 멀티 테넌트 FastAPI 서비스. Claude Code 후크가
  `localhost:8000/evaluate` 로 POST. 풀 M1–M17 surface (서명, ATMU
  (Agent Telemetry Management Unit), cost ledger, HAM, Burn-in).
- **Plugin (`local`) 모드** (신규, Solo Free) — 단일 개발자용 in-process 후크.
  서비스 / HTTP / API 키 불필요. 후크가 firewall 파이프라인 (310→311→312→
  320→330→335→340) 을 자체 프로세스에서 실행.

설치:
```bash
uv run aegis install --mode sidecar   # 기본
uv run aegis install --mode local     # Solo Free (dummy embedding+judge 강제)
```

두 모드는 같은 ATV-2080-v1 30-subfield 스키마와 같은 firewall 룰 (step310
+ step311 donor rules + step312 normalize + …) 을 공유합니다. v2.0.0 은
12-incident donor KPI 를 sidecar 모드 기준 12/12 strict pass.

## Plugin Mode 작업 시 알아둘 것

- **Local hook 환경 변수 강제**: `aegis install --mode local` 은
  `~/.claude/settings.json` 명령어에 `AEGIS_EMBEDDING_PROVIDER=dummy` +
  `AEGIS_JUDGE_PROVIDER=dummy` 를 자동으로 prepend (Solo Free 컨트랙트).
  사용자가 OpenAI / Haiku 를 쓰고 싶다면 settings.json 을 수동 편집.
- **`tools/aegis_cli.py` 직접 수정 시**: 활성 hook 의 haiku judge 가
  "self-modification of security infrastructure" 로 BLOCK 합니다 (정상
  동작). SESSION_HANDOFF §8.3 의 protocol 따라 `docker compose stop` →
  편집 → `docker compose start`.
- **신규 step 추가 시**: `src/aegis/firewall/core.py default_steps()` 의
  순서를 직접 편집해야 활성화됩니다 (`step311` 이 그 예시).
- **donor 자산 이식 패턴**: 모든 v2.0 P0 자산은 D-번호 (D1~D6) 단위로
  commit 분리. 상세 매핑은 `INTEGRATION_PLAN.md` §3.1 참조.
