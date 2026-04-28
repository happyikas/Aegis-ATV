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
  `localhost:8000/evaluate` 로 POST. 풀 M1–M17 surface (서명, ATMU, cost
  ledger, HAM, Burn-in).
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
