# 사고 대응 시나리오 — 실행 가능 버전

[`WHITEPAPER.md` §5](../../WHITEPAPER.md) 의 7개 사고 대응 시나리오를
**실제로 실행 가능한 bash 스크립트**로 재현합니다. 각 스크립트는
endpoint를 직접 호출하고 PASS/FAIL 자동 검증합니다.

```
demo/scenarios/
├── _lib.sh                          공통 helper (color, assert, build_payload)
├── _payload.py                      ATVInput JSON payload builder
├── _ham.py                          HAM endpoint helper (memory/recall/forget/ground/stats)
├── scenario_a_db_drop.sh            §5.1 production DB drop attempt
├── scenario_b_cost_spike.sh         §5.2 token cost spike detection
├── scenario_c_prompt_injection.sh   §5.3 external PDF injection
├── scenario_d_supply_chain.sh       §5.4 config tamper + burn_in_id boundary
├── scenario_e_insider_drift.sh      §5.5 insider behavioral drift
├── scenario_f_ham_tamper.sh         §5.6 wiki tamper → HAM ground inconsistency
├── scenario_g_multi_agent.sh        §5.7 multi-agent cascade
└── run_all.sh                       master runner with PASS/FAIL summary
```

## 빠른 시작

```bash
# 1. Aegis 서비스 부팅 (한 번만)
docker compose up -d

# 2. 모든 시나리오 실행
bash demo/scenarios/run_all.sh

# 또는 개별 실행
bash demo/scenarios/scenario_a_db_drop.sh
```

## 실행 결과 (현 main, 2026-04-22 기준)

```
════════════════════════════════════════════════════════════════════════
  FINAL SUMMARY (elapsed: 92s)
════════════════════════════════════════════════════════════════════════

  PASSED (7):
    ✓ scenario_a_db_drop.sh
    ✓ scenario_b_cost_spike.sh
    ✓ scenario_c_prompt_injection.sh
    ✓ scenario_d_supply_chain.sh
    ✓ scenario_e_insider_drift.sh
    ✓ scenario_f_ham_tamper.sh
    ✓ scenario_g_multi_agent.sh

  All 7 scenarios PASSED. ✅
```

## 각 시나리오의 5단계 구조

모든 시나리오는 동일한 사고 대응 구조를 따릅니다:

```
   ┌────────────┐      ┌────────────┐      ┌────────────┐
   │ ① 검출     │ ───→ │ ② 격리     │ ───→ │ ③ 포렌식   │
   │ (Detection)│      │ (Contain)  │      │ (Forensics)│
   └────────────┘      └────────────┘      └─────┬──────┘
                                                 │
   ┌────────────┐      ┌────────────┐            │
   │ ⑤ 사후     │ ←─── │ ④ 복구     │ ←──────────┘
   │ (Postmortem│      │ (Recovery) │
   └────────────┘      └────────────┘
```

각 단계는 실제 endpoint를 호출하여 결과를 검증합니다.

## 시나리오별 핵심 검증

### A — Production DB Drop (§5.1)
- ① regex catch (`DROP TABLE`) → BLOCK in ~5ms
- ② step 315 violations (3회) → AID quarantine
- ③ audit chain `chain_valid: True`, journal `tampered_count: 0`
- ④ admin token release → AID 다시 정상 호출 가능
- ⑤ Burn-in L4 baseline 자동 재캘리브레이션

### B — Token Cost Spike (§5.2)
- 5건의 escalating cost 호출 ($0.01 → $25)
- Cost Attestation Ledger에 모두 기록 (별도 키)
- 마지막 호출은 step 335에 의해 REQUIRE_APPROVAL/BLOCK

### C — External PDF Prompt Injection (§5.3)
- 3개 HAM entry 사전 적재 (정상 2 + 오염 1)
- 외부 API 호출 시도 → step 310 BLOCK
- HAM `ground` 으로 의사결정 ↔ 메모리 cryptographic binding
- HAM `forget` 으로 오염 entry tombstone (영구 삭제 X)
- 후속 recall 에서 tombstoned entry 사라짐

### D — Supply Chain (§5.4)
- 컨테이너 안의 `policies/aid_region.json` 변조 (docker exec)
- 서비스 재시작 → `burn_in_id` 와 `L4_config hash` 모두 변경
- audit chain header 에 새 burn_in_id 박힘
- rollback 후 burn_in_id 가 원래 값으로 복귀

### E — Insider Behavioral Drift (§5.5)
- Phase 1: 10건의 정상 read_file (slow & steady)
- Phase 2: 5건의 burst 공격 패턴 (sudo, exfil, delete)
- 4/5 burst 가 BLOCK 또는 escalate
- audit chain timestamp gap 분석 — bimodal distribution 검출

### F — HAM Tamper (§5.6)
- 정상 entry (`q3_revenue=$2.4M`) + 변조 entry (`$24M, 10x`)
- 둘 다 같은 tags → recall 에 둘 다 surface
- inconsistency 자동 검출 (10x ratio)
- 변조 entry tombstone 후 ground 가 missing 으로 보고

### G — Multi-Agent Cascade (§5.7)
- A 손상 → B 의 retrieval index 에 inter-agent 메시지 삽입
- B 가 retrieve 후 외부 API 호출 → BLOCK
- A 도 직접 시도 → 동일 패턴으로 BLOCK
- HAM ground 로 cascade path 추적 (B's call ← A's planted message)

## 사전 조건

- Docker (OrbStack 권장) — `docker compose up -d` 가 동작해야 함
- Python 3.11+ — JSON 페이로드 / endpoint 응답 파싱
- `policies/aid_region.json` 의 `demo-tenant:read-only-role` 정책
  (저장소에 이미 포함됨)

## 환경 변수

| 변수 | 기본 | 효과 |
|---|---|---|
| `AEGIS_URL` | `http://localhost:8000` | 서비스 위치 |
| `AEGIS_ADMIN_TOKEN` | `dev-admin-token` | scenario A 의 admin release용 |

## 새 시나리오 추가하기

1. `_lib.sh` 의 helper 사용 (`scenario`, `stage`, `info`, `assert_eq` 등)
2. `_payload.py` 또는 `_ham.py` 로 endpoint 호출
3. 5단계 구조 (검출 → 격리 → 포렌식 → 복구 → 사후) 따르기
4. `scenario_end` 로 마무리 (자동 PASS/FAIL 표시)
5. `run_all.sh` 의 `SCENARIOS=()` 배열에 추가

## 디버깅

각 시나리오는 stderr에 다음을 출력합니다:
- `▸ stage` — 시나리오 단계 마커
- `→ info` — 변수값 / endpoint 응답
- `✓` / `✗` — assertion 결과
- `# note` — 보충 설명

실패 시 마지막 5–10줄을 보면 어디서 어떤 assertion 이 깨졌는지 즉시 파악
가능합니다.

## CI 통합 (TODO)

`.github/workflows/ci.yml` 에 새 job 추가:

```yaml
scenarios-e2e:
  runs-on: ubuntu-latest
  needs: [docker-build]
  steps:
    - uses: actions/checkout@v4
    - run: docker compose up -d --build
    - run: until curl -sf localhost:8000/healthz; do sleep 1; done
    - run: bash demo/scenarios/run_all.sh
```

이렇게 두면 **PR마다 7개 사고 시나리오가 자동 실행** 되어 회귀가
즉시 감지됩니다.
