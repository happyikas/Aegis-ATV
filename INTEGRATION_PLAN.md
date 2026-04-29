# INTEGRATION_PLAN.md — MVP/ ↔ aegis-mvp/ 통합 계획

**작성일:** 2026-04-26  ·  **목표 릴리스:** v2.0.0  ·  **예상 기간:** 2–3일  ·  **실행 환경:** Claude Code on macOS

---

## 0. TL;DR

이번 코워크 세션에서 만든 `aegis-mvp/` (v1.0.0, 142 files, 62 tests, Claude Code 플러그인 형태)를 기존 `MVP/` (T2 사이드카, 455 tests, M1–M17, 49-page whitepaper)에 **통합**한다. 결과는 단일 코드베이스 v2.0.0:

- **사이드카 모드** (기존 MVP/) — 조직·다중 테넌트 배포용 FastAPI 서버
- **플러그인 모드** (aegis-mvp/ 기여) — 개발자 1명이 5분 안에 install해서 즉시 차단 보여주는 Claude Code 플러그인

두 모드는 **같은 ATV·ATMU (Agent Telemetry Management Unit)·Burn-in 코어**를 공유한다.

---

## 1. 결정 사항 (이미 확정)

| 항목 | 결정 | 근거 |
|---|---|---|
| **Master** | `MVP/` | 더 성숙 (M1–M17), 특허 v7.10 정확 매핑, 455 tests, 49-page WHITEPAPER, demo recording 등 모든 자산 보유 |
| **Donor** | `aegis-mvp/` | 6개 신규 자산 기여 (아래 §3) |
| **ATV 차원** | **2080-d 유지** (MVP/가 master) | 특허 Appendix A 매핑. aegis-mvp/의 256-d는 폐기, 단 인코더 로직만 일부 차용 |
| **Python** | **3.12 통일** | MVP/는 3.11, aegis-mvp/는 3.12. CI matrix는 3.11/3.12 둘 다 |
| **서버 모드** | 기본 = FastAPI 사이드카 (MVP/ 그대로). 옵션 = `aegis_local` 모듈 모드 (aegis-mvp/ 스타일, 네트워크 없음) | Solo Free 티어용 |
| **Plugin packaging** | aegis-mvp/의 `.claude-plugin/plugin.json` + `aegis install` CLI 채택 | Claude Code 표준 |
| **버전 점프** | `v1.x` (MVP/) → **`v2.0.0`** | aegis-mvp/ 통합은 BREAKING |

---

## 2. MVP/가 이미 가진 자산 (그대로 보존)

이 목록은 절대 건드리지 않는다 — 회귀 발생 시 즉시 abort.

| ID | 자산 | 위치 |
|---|---|---|
| M1–M7 | FastAPI + ATV + 7-step Firewall + sLLM + Ed25519/Merkle + audit + code attestation | `src/aegis/{api,atv,firewall,judge,sign,audit,attest}/` |
| **M8** | **ATV-2080-v1 30 subfield** (특허 Appendix A) | `src/aegis/schema.py` |
| M9 | Firewall 350/360/370 분리 | `src/aegis/firewall/step35*.py` |
| M10 | ATMU 2PC + WAL intent log | `src/aegis/atmu/` |
| M11 | 5-layer Burn-in + 4-phase | `src/aegis/burnin/` |
| M12 | Cost Attestation Ledger (별도 키, Claim 34) | `src/aegis/cost/` |
| M13 | sLLM attribution head | `src/aegis/judge/` |
| M14 | AID auth + circuit breaker | `src/aegis/firewall/{step315_aid_auth,circuit_breaker}.py` |
| M15 | AES-256-GCM journal | `src/aegis/audit/encrypted_journal.py` |
| M16 | HAM L3+L4 (6 ops) | `src/aegis/ham/` |
| M17 | TEE attestation (mock) | `src/aegis/attest/tee_quote.py` |
| — | 49-p WHITEPAPER PDF + 13-slide DECK | `docs/build/` |
| — | demo.gif + asciinema + 9 PNG + narration | `demo/recording/` |
| — | 7 incident scenarios | `demo/scenarios/` |
| — | Self-dogfood 28 catch + Phase A/B 비교 | `data/dogfood/` + `docs/DOGFOOD*.md` |
| — | Docker compose + GitHub Actions CI | `docker-compose.yml`, `.github/workflows/ci.yml` |
| — | 4 정책 JSON | `policies/` |

---

## 3. aegis-mvp/ 기여 자산 (이식 대상)

우선순위 순. 각 항목은 한 commit으로.

### 3.1 P0 (반드시 이식)

| # | 자산 | aegis-mvp/ 위치 | MVP/ 목적지 | 비고 |
|---|---|---|---|---|
| **D1** | Claude Code payload 어댑터 | `claude_hooks/payload.py` | `tools/aegis_payload.py` | `tool_name/tool_input` ↔ `/evaluate` 변환 |
| **D2** | Plugin manifest | `.claude-plugin/plugin.json` | `.claude-plugin/plugin.json` | 그대로 복사, 버전만 `2.0.0` |
| **D3** | `aegis` CLI (15 subcommand) | `claude_hooks/cli.py` | `tools/aegis_cli.py` | `aegis install/status/cost/health/replay/rollback/burnin/budget`. 기존 `tools/install_hook.py`와 통합 |
| **D4** | 4 rollback strategies | `rollback/strategies/{file,shell,git,mcp}.py` | `src/aegis/rollback/strategies/` | 신규 모듈. M9의 step370 exec와 연결 |
| **D5** | Cost transcript 파서 | `cost/transcript.py` | `src/aegis/cost/transcript.py` | M12 Cost Ledger와 합류 |
| **D6** | Stop hook 자동 import | `claude_hooks/session_end.py` | `tools/hooks/session_end.py` | Claude Code Stop event 처리 |

### 3.2 P1 (권장 이식)

| # | 자산 | aegis-mvp/ 위치 | MVP/ 목적지 | 비고 |
|---|---|---|---|---|
| D7 | 세션-격리 malfunction (per-tool 한도) | `monitor/malfunction.py` | `src/aegis/monitor/malfunction.py` | M14 quarantine과 직교 보완 |
| D8 | Burn-in retrain 도구 | `burnin/retrain.py` | `src/aegis/burnin/retrain.py` | M11 4-phase에 sanity-check + revert 추가 |
| D9 | Policy replay engine | `replay/engine.py` | `src/aegis/api/replay.py` | 기존 `/forensic/replay` 확장 |
| D10 | Budget policy hot-reload | `cost/budget.py` | `src/aegis/cost/budget.py` | M12 ledger와 연동 |
| D11 | 신규 룰 2개 (`cost_overflow`, `malfunction_pattern`) | `atmu/rules/` | MVP/ policy JSON으로 변환 | 룰셋 컨벤션 차이로 변환 필요 |

### 3.3 P2 (문서·산출물)

| # | 자산 | aegis-mvp/ 위치 | MVP/ 목적지 |
|---|---|---|---|
| D12 | CLAUDE.md (project standing instructions) | `CLAUDE.md` | `CLAUDE.md` (기존 1862 byte를 v2.0으로 확장) |
| D13 | RUNBOOK (10분 데모) | `RUNBOOK.md` | `docs/RUNBOOK.md` |
| D14 | 한글 매뉴얼 docx | `AegisData_Manual_KR_v1.0.0.docx` | `docs/Manual_KR_v2.0.docx` |
| D15 | RELEASE notes 컨벤션 | `RELEASE.md` | `docs/RELEASE_v2.0.md` |
| D16 | CHANGELOG 양식 | `CHANGELOG.md` | 기존 commit history와 합류 |

---

## 4. 8단계 페이즈 플랜

각 페이즈 끝에 **gate criteria**가 있다. 통과해야 다음 진행.

### Phase 0 — 사전 점검 (15분)

```bash
cd ~/Library/CloudStorage/OneDrive-Personal/MVP
git status                                # working tree clean
git checkout -b feat/v2.0-merge
uv run pytest -q | tail -3                # 455 passed 확인
uv run mypy src 2>&1 | tail -2            # clean
uv run ruff check . 2>&1 | tail -2        # clean
docker compose up -d
until curl -sf localhost:8000/healthz; do sleep 1; done
```

**Gate:** 위 모두 통과. 실패 시 통합 시작 금지.

### Phase 1 — Donor 추출 (30분)

aegis-mvp/ 전체를 MVP/ 안의 임시 디렉터리로 복사 → 점진 이식.

```bash
# aegis-mvp 압축 풀기 (코워크에서 받은 zip)
mkdir -p _donor && cd _donor
unzip ~/Downloads/aegis-mvp-v1.0.0.zip
cd ../

# 디렉터리 구조 확인
ls _donor/aegis-mvp/
```

**Gate:** `_donor/aegis-mvp/` 안에 142 file 존재 확인.

### Phase 2 — P0 자산 이식 (3–4시간)

D1부터 D6까지 한 commit씩.

```bash
# D1 — payload adapter
cp _donor/aegis-mvp/claude_hooks/payload.py tools/aegis_payload.py
# imports를 MVP/ 스타일로 조정 (atv → src.aegis.atv 등)
# 단위 테스트 추가: tests/unit/test_aegis_payload.py
git add tools/aegis_payload.py tests/unit/test_aegis_payload.py
git commit -m "feat(plugin): D1 — Claude Code payload adapter (tool_name/tool_input ↔ /evaluate)"

# D2 — plugin manifest
mkdir -p .claude-plugin
cp _donor/aegis-mvp/.claude-plugin/plugin.json .claude-plugin/
# version을 2.0.0으로 수정
git add .claude-plugin/
git commit -m "feat(plugin): D2 — plugin.json manifest for Claude Code"

# D3 — aegis CLI
cp _donor/aegis-mvp/claude_hooks/cli.py tools/aegis_cli.py
# 기존 tools/install_hook.py 기능을 cmd_install로 흡수
# pyproject.toml의 [project.scripts]에 aegis = "tools.aegis_cli:main" 추가
git add tools/aegis_cli.py pyproject.toml
git commit -m "feat(plugin): D3 — aegis CLI (15 subcommands) replacing install_hook.py"

# D4 — rollback strategies (구조만, 통합은 Phase 4에서)
mkdir -p src/aegis/rollback
cp -r _donor/aegis-mvp/rollback/strategies src/aegis/rollback/
cp _donor/aegis-mvp/rollback/snapshot.py src/aegis/rollback/snapshot.py
git add src/aegis/rollback/
git commit -m "feat(rollback): D4 — 4 rollback strategies (file/shell/git/mcp)"

# D5 — cost transcript parser
cp _donor/aegis-mvp/cost/transcript.py src/aegis/cost/transcript.py
# 기존 src/aegis/cost/__init__.py에 export 추가
git commit -m "feat(cost): D5 — Claude Code transcript .jsonl auto-parser"

# D6 — Stop hook
mkdir -p tools/hooks
cp _donor/aegis-mvp/claude_hooks/session_end.py tools/hooks/session_end.py
git commit -m "feat(plugin): D6 — Claude Code Stop hook auto cost-import"
```

**Gate (각 commit 후 실행):**
- `uv run pytest -q | tail -3` → 455+ passed (회귀 0)
- `uv run ruff check .` → clean
- `uv run mypy src` → clean

회귀 발생 시 `git reset --hard HEAD~1` 후 원인 분석.

### Phase 3 — ATV 어댑터 (2시간)

aegis-mvp/의 256-d 입력을 MVP/의 2080-d 30-subfield로 매핑하는 어댑터 작성. 두 가지 방향:

- **Forward**: `(tool, args)` → `ATVInput` (MVP/ 기존)
- **Backward**: 256-d 임베딩을 2080-d 안의 한 subfield(`agent.behavior_embedding`)로 채워넣기

```python
# src/aegis/atv/adapter.py 신규
def from_claude_code_payload(req: dict) -> ATVInput:
    """Claude Code hook payload → ATV-2080 인스턴스."""
    # tool_name / tool_input → MVP/'s 30-subfield 매핑
    ...
```

**Gate:**
- 단위 테스트: `tests/unit/test_atv_adapter.py` 통과
- e2e: aegis-mvp/의 12-incident 패턴이 MVP/'s `/evaluate`로 들어와 동일하게 차단되는지 확인

### Phase 4 — 테스트 마이그레이션 (3시간)

aegis-mvp/의 62 tests 중 **MVP/에 의미 있는 50–55개**만 이식. 나머지는 중복 (이미 MVP/에 더 좋은 버전 있음).

```bash
# 디렉터리: tests/plugin/  (신규 카테고리)
mkdir -p tests/plugin/{incidents,integration,perf,strategies}

# 이식 대상 (예시)
cp _donor/aegis-mvp/tests/integration/test_claude_code_protocol.py tests/plugin/integration/
cp _donor/aegis-mvp/tests/test_phase4_strategies.py tests/plugin/strategies/
cp _donor/aegis-mvp/tests/test_session_isolation.py tests/plugin/integration/
cp _donor/aegis-mvp/tests/test_burnin_retrain.py tests/plugin/
cp _donor/aegis-mvp/tests/test_transcript_parser.py tests/plugin/

# import 경로 수정 (atv → src.aegis.atv 등) — sed 또는 수동
# pytest 마커 통일: @pytest.mark.plugin

uv run pytest tests/plugin/ -q
```

**Gate:**
- 전체: `uv run pytest -q` → 505–510 passed
- plugin only: `uv run pytest -m plugin -q` → 50+ passed

### Phase 5 — Plugin 모드 packaging (2시간)

`aegis install` 명령이 다음을 자동 수행:

1. `.claude-plugin/plugin.json` 검증
2. `~/.claude/settings.json`에 hook 등록
3. 사이드카 mode (default) vs 로컬 mode 선택 안내:
   - `--mode sidecar`: hook이 `curl localhost:8000/evaluate`
   - `--mode local`: hook이 in-process Python (Solo Free)

```bash
# 작동 검증 (자기 자신에게 dogfood)
aegis install --mode sidecar
# Claude Code 재시작
# Bash로 'rm -rf /' 시도 → BLOCK 확인
aegis status
# → intents 1, blocks 1 (rule:destructive_fs)
```

**Gate:**
- 라이브 hook 발화 → 차단 확인
- `aegis status` 정상 출력
- 기존 `tools/install_hook.py`로 설치한 사용자도 호환 (마이그레이션 안내 추가)

### Phase 6 — 문서 통합 (1시간)

```bash
# CLAUDE.md를 aegis-mvp/ 버전 기준으로 확장 (MVP/의 기존 1862 byte 룰 + aegis-mvp/의 12 섹션)
# RUNBOOK.md → docs/
# Manual_KR docx → docs/
# CHANGELOG에 v2.0.0 entry 추가

# WHITEPAPER에 §6 (Plugin mode) 신규 섹션 추가
# §M18 (M18-M22 진행 상황) 갱신
bash tools/whitepaper/build_pdf.sh
bash tools/deck/build_pdf.sh
```

**Gate:**
- WHITEPAPER.pdf v2.0 생성 (페이지 수 49 → 55 예상)
- DECK 13장 → 15장 (Plugin mode 슬라이드 + v2.0 KPI 슬라이드 추가)

### Phase 7 — 릴리스 v2.0.0 (1시간)

```bash
# 최종 검증
uv run pytest -q                          # 510+ passed
uv run mypy src                           # clean
uv run ruff check .                       # clean
docker compose up -d
bash demo/scenarios/run_all.sh            # 7/7 PASS
aegis install --mode sidecar
# Claude Code 재시작 → 라이브 차단 5건 검증

# Commit + tag
git add -A
git commit -m "release: v2.0.0 — aegis-mvp plugin merged into MVP sidecar"
git tag -a v2.0.0 -m "v2.0.0 — sidecar + Claude Code plugin unified"
git push origin feat/v2.0-merge
git push origin v2.0.0

# PR 생성
gh pr create --title "v2.0.0: aegis-mvp plugin integration" \
  --body "Merges aegis-mvp v1.0.0 (Claude Code plugin) into MVP T2 sidecar. Closes #X."
```

**Gate:**
- CI 모든 job green
- Squash merge to main
- v2.0.0 태그 push 완료

### Phase 8 — Mac 라이브 데모 + Rollback drill (30분)

```bash
# Plugin mode dogfood: 이 세션에서 진짜 차단 5건 캡처
aegis install --mode local
# Claude Code 재시작
# 의도적으로 위험 명령 5건 시도 → 모두 차단 확인
aegis status                              # 정확한 카운트 확인
aegis verify-audit                        # 전체 서명 검증

# Rollback drill (실패 시나리오)
git checkout main
git revert v2.0.0..HEAD                   # 시뮬레이션, 실제론 안 함
# → 1.x로 복구 가능함을 확인
```

**Gate:** 모두 통과 시 v2.0 release notes 발행.

---

## 5. 테스트 전략

| 단계 | 명령 | 기대치 |
|---|---|---|
| Baseline (Phase 0) | `pytest -q` | 455 passed |
| 각 P0 commit 후 | `pytest -q` | 455 + n passed (회귀 0) |
| Phase 4 완료 후 | `pytest -q` | 505–510 passed |
| Phase 7 (release) | `pytest -q` | 510+ passed |
| Plugin only | `pytest -m plugin -q` | 50+ passed |
| Sidecar e2e | `bash demo/scenarios/run_all.sh` | 7/7 PASS |
| Live Claude Code | manual (rm -rf, force push, DROP) | 모두 BLOCK |

---

## 6. 위험 항목 (Risk Register)

| 위험 | 확률 | 영향 | 대응 |
|---|---|---|---|
| ATV 256-d → 2080-d 매핑이 의미 손실 | 중 | 중 | Phase 3에서 단위 테스트 강화. `behavior_embedding` subfield로만 한정 |
| Python 3.11 ↔ 3.12 호환 문제 | 낮 | 중 | CI matrix에 둘 다 포함. dev 환경은 3.12로 통일 |
| 룰 카테고리 충돌 (e.g., `cost_overflow`) | 중 | 낮 | MVP/는 정책 JSON 기반, aegis-mvp/는 Python 클래스. JSON으로 변환 |
| `evaluate()` 함수명 충돌 | 낮 | 낮 | aegis-mvp/의 evaluator는 `tools/aegis_payload.evaluate_local()`로 rename |
| Burn-in 모델 형식 불일치 | 중 | 낮 | aegis-mvp/의 IsolationForest는 M11의 5-layer baseline 안의 한 layer로 흡수 |
| Claude Code hook 양식 변경 | 낮 | 높 | Anthropic 공식 docs 기반. 변경 시 D1 어댑터만 수정하면 됨 |
| 의존성 충돌 (numpy/sklearn 버전) | 낮 | 중 | `uv.lock` 재생성 + CI에서 검증 |

---

## 7. v2.0.0 릴리스 체크리스트

- [ ] `uv run pytest -q` → 510+ passed
- [ ] `uv run mypy src` → clean
- [ ] `uv run ruff check .` → clean
- [ ] `docker compose up -d` → /healthz 200
- [ ] `bash demo/scenarios/run_all.sh` → 7/7 PASS
- [ ] `aegis install --mode sidecar` → Claude Code 라이브 차단 검증 5건
- [ ] `aegis install --mode local` → 라이브 차단 검증 5건
- [ ] `aegis verify-audit` → 모든 서명 검증
- [ ] `aegis status` 정상 출력
- [ ] WHITEPAPER.pdf v2.0 (55+ p) 빌드
- [ ] PITCH_DECK.pdf v2.0 (15장) 빌드
- [ ] CHANGELOG에 v2.0.0 entry
- [ ] `git tag v2.0.0 && git push origin v2.0.0`
- [ ] GitHub Release 작성 (binaries: WHITEPAPER, DECK, demo.gif)
- [ ] SESSION_HANDOFF.md 업데이트

---

## 8. Claude Code 첫날 시작 프롬프트 (복사·붙여넣기)

새 Claude Code 세션을 열고 그대로 붙여넣으면 즉시 작업 시작.

```
이 프로젝트는 AegisData T2 사이드카 MVP다. 오늘 하루 동안 INTEGRATION_PLAN.md 에 따라
v2.0.0 통합 작업을 시작한다. 먼저 다음을 순서대로 수행해라:

1. SESSION_HANDOFF.md 와 CLAUDE.md 와 INTEGRATION_PLAN.md 를 읽어라.
2. Phase 0 사전 점검을 실행하고 결과를 보고해라.
3. _donor/ 디렉터리에 aegis-mvp-v1.0.0.zip 을 풀어라.
   (zip 파일은 ~/Downloads 에 있다. 없으면 안내해라.)
4. Phase 1까지 완료하고 다음 단계 결정을 내가 하도록 보고해라.

규칙:
- 각 단계 완료 후 반드시 pytest 회귀 검증 (455+ passed 유지).
- 회귀 발생 시 즉시 git reset --hard 후 원인 분석.
- 임의로 Phase를 건너뛰지 마라.
- D-번호 (D1~D16) 단위로 commit 분리.
```

추가 권장 (optional):

```
이 통합은 BREAKING change 다. v1.x 사용자를 위한 마이그레이션 가이드를
docs/MIGRATION_v1_to_v2.md 에 작성해라. 다음을 포함:
- 기존 tools/install_hook.py → aegis install 전환
- /evaluate API 응답 schema 변경 사항
- 새 환경 변수 (AEGIS_MODE=sidecar|local)
```

---

## 9. 통합 후 다음 마일스톤 (참고)

v2.0.0 릴리스 직후 진행 가능:

- **M18** ML-DSA dual-signing (Claim 25, oqs-python) — 3일
- **M19** RAPL/NVML HW counters — Linux 서버 필요, 5일
- **M20** FPGA sLLM — Xilinx Versal AI Edge 필요
- **M21** HW tag comparator — bare-metal IOMMU 필요
- **M22** CSD integration — Solidigm CSD eval 필요

이 5개는 **T3 하드웨어 티어**로, 별도 자금 라운드 필요.

---

## 10. 인계 시 알아야 할 컨텍스트

- 사용자: Chanik Park, 시스템 아키텍트, MAS 상용화 + HW/SW codesign
- 펀딩 시나리오: B+ (Solo SW MVP 3M → demo 1M → Solo HW PoC 3M → Series A $10–15M @ $60–80M pre)
- 특허: US Provisional `ATV_v7_10` (84 claims), 출원 준비됨
- 3-month plan (May–Jul 2026): Programs · Plugin MVP · SV Partnerships, $80.5K
- aegis-mvp 코워크 산출물 위치: `~/Downloads/aegis-mvp-v1.0.0.zip` (또는 OneDrive/SW Codesign 폴더)
- 한글 매뉴얼: 코워크 산출물 `AegisData_Manual_KR_v1.0.0.docx` 그대로 사용 가능

---

*이 파일은 v1.0.0 통합 계획용이며, v2.0.0 릴리스 직후 archive 처리.*
*문의 / 결정 변경 시 Chanik에게 직접 보고.*
