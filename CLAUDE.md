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
