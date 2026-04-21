# PLAN v2 — AegisData MVP 재정비 (Patent v7.10 정합)

**참조 명세**: `AegisData_US_Provisional_Patent_ATV_v7_10.docx` (Draft 2026‑04‑21)
**대상 티어**: T2 (software-only). T3 하드웨어 항목은 API/스키마 자리만 잡고 본 MVP 범위 밖.
**전제**: 현재 `main` 브랜치의 Milestone 1‑7 + Burn-in/attestation + classifier + Theater까지 완성된 상태 (commit 20여 개). 이 문서는 **특허 v7.10과의 델타**를 식별하고 후속 Milestone 8‑16을 정의한다.

---

## 진행 상태 (2026‑04‑21 업데이트)

| Milestone | 상태 | 주요 성과 | 신규 테스트 |
|---|---|---|---|
| **M8** ATV‑2080‑v1 30 subfield 스키마 | ✅ 완료 | `schema.py` + `atv/builder.py` 전면 재작성. 19 SW encoder + HW band zero‑fill. `CostEfficiencyMetrics` 16 slot. ATVHeader에 schema_version/tier_profile/cost_attestation_profile/atv_hash 추가. **Breaking change** (cost_estimate shape) | +12 (177 → 176 total) |
| **M9** Firewall 350/360/370 분리 | ✅ 완료 | `step350_approval` (notification dispatch), `step360_audit` (sign+append+cost_attestation_hint), `step370_exec` (PROCEED/SUPPRESS/DEFER 주석). `api/evaluate.py` 슬림화 | +9 (185) |
| **M10** ATMU + Write‑Ahead Intent Log + Tool Outcome | ✅ 완료 | `atmu/{state_machine,intent_log,checkpoint,compensating}` + `POST /tool-outcome`. 2PC tentative→prepared→committed 흐름. 7 transaction states (APPENDIX B) + 합법 transition만 허용. 100‑record 동시성 안전 | +37 (222) |
| **M11** 5‑layer Burn‑in + 4‑phase graduation | ✅ 완료 | `burnin/{phases,controller}` + `GET /burnin-status` + `POST /burnin/graduate` + `POST /burnin/label`. L1..L5 자동 슬롯 생성, ¶[0075] threshold (1000 samples / TPR≥0.95 FPR≤0.02 precision≥0.90 / override≤5%) | +21 (243) |
| **M12** Cost Attestation Ledger + 3 divergence | 🔜 다음 | — | — |
| **M13** sLLM attribution head | 🔜 | — | — |
| **M14** AID auth table + circuit breaker | 🔜 | — | — |
| **M15** 암호화 ATV 저널 + forensic replay | 🔜 | — | — |
| **M16** HAM‑lite (stretch) | 🔜 | — | — |

**현재 도커 컨테이너에 살아있는 새 endpoint**:
`POST /tool-outcome` · `GET /burnin-status` · `POST /burnin/graduate` · `POST /burnin/label`

**`/evaluate` step_traces 추가 항목**:
`step350_approval.dispatch` (REQUIRE_APPROVAL 시) · `step360_audit.sign_and_append` (`cost_attestation_hint` boolean 포함) · `step370_exec.annotate` (PROCEED/SUPPRESS/DEFER) · `aegis.atmu.intent_log` (record_id 노출) · `aegis.burnin.composite_score` (composite anomaly score)

**테스트**: pytest 243 passed · ruff clean · mypy strict 46 src files · 도커 e2e 모든 신규 path 통과

---

## 0. Diff — 한 페이지 요약

| 영역 | 현재 MVP | 특허 v7.10 | 조치 |
|---|---|---|---|
| ATV 크기 | 2080‑D ✓ | 2080‑D ✓ | — |
| ATV 서브필드 수 | **8개** | **30개** (SW 19 + HW 11) | **리팩토링** (M8) |
| 서브필드 인덱스 | MVP 고유 배치 | Appendix A 고정 매핑 | **전면 교체** (M8) |
| `cost_efficiency_metrics` 1864..1879 | ✓ 위치 일치, 구조 불일치 | 16‑D 명세 s‑1..s‑16 | **16개 필드 정확히 채우기** (M8) |
| Firewall 5 steps (310‑340) | ✓ | ✓ | — |
| Firewall steps 350/360/370 | `/evaluate` 안에 뭉침 | 명시적 분리 (notify→audit→exec) | **분리 리팩토링** (M9) |
| sLLM judge | Haiku API (범용) | 0.1‑1B 전용 분류기, 3‑head (verdict/confidence/**attribution**) | attribution head만 소프트웨어로 (M13) |
| Burn-in | 코드 해시 L3‑L5 (attestation 의미) | **통계 baseline 5‑layer + 4‑phase graduation** | **이름 교체 + 새 모듈 추가** (M11) |
| ATMU / Write‑Ahead Intent Log | **없음** | 2PC + 7개 transaction state marker | **신규 구현** (M10) |
| Cost Attestation Ledger | audit log 안에 섞임 | **별도 원장**, selective disclosure | **신규 구현** (M12) |
| Dual‑band cost + 3 divergence metrics | SW‑only | SW + HW + token↔FLOPs / mem / $ divergence | **software‑side divergence stub** (M12) |
| AID tagging + circuit breaker | aid 필드만 있음, 인가 없음 | HW tag enforcement + 회로 차단 | **software 인가 테이블** (M14) |
| 암호화 ATV 저널 + forensic replay | plaintext JSONL/SQLite | AEAD 암호화 + replay engine | **신규 구현** (M15) |
| Hierarchical Agent Memory | 없음 | 4‑level (L1 HBM / L2 CXL / L3 CSD‑DRAM / L4 NAND) | L3/L4 software emulation (M16, stretch) |
| Temporal Intelligence / causal DAG | `time_ns()` scalar | monotonic ns + causal graph | M15 replay 안에 scalar causal ordering |
| 특허 Claim 매핑 | 1, 2, 3 부분, 15, 23 부분 | 1‑40 | 아래 §7 매트릭스 |

---

## 1. 이미 patent‑align된 구성

| 구성 | 위치 | 상태 |
|---|---|---|
| 2080‑D float32 tensor + 고정 인덱스 개념 | `src/aegis/schema.py` | ✓ 크기만 맞음; 배치 교체 필요 |
| 헤더 메타데이터 (trace_id/span_id/tenant_id/aid/ats/timestamp_ns) | 동 | ✓ 특허 ¶[0049]와 부합. `node_id/pod_id/schema_version/tier_profile/cost_attestation_profile/atv_hash/signature` 추가 필요 |
| SHA3‑256 commitment + Ed25519 서명 | `src/aegis/sign/ed25519.py` | ✓ 특허 Section 4 부합 (ML‑DSA는 Claim 25 선택사항) |
| Merkle 체인 감사 로그 | `src/aegis/audit/sqlite_store.py`, `jsonl_store.py` | ✓ 특허 Section 4 + tamper‑evident 요건 부합 |
| Firewall 5 checks (310..340) | `src/aegis/firewall/step*.py` | ✓ 특허 Section 5 ¶[0056]‑[0060] 부합 |
| `REQUIRE_APPROVAL` verdict | `/approve` endpoint | △ 특허 step 350 (notify→await→commit/abort) 형태로 재구성 필요 |
| Pre‑LLM safety classifier | `tools/aegis_safety.py` | ✓ 특허 Appendix A의 tool_arg_inspection + prompt_structure + output_content_fingerprint의 input 소스 역할 |
| /attestation endpoint | `src/aegis/api/attestation.py` | ✓ API 자체는 유지. 내부는 코드 해시(L3‑code)가 특허 5‑layer Burn‑in과 **다른 개념**이므로 리네이밍 (M11) |
| Theater / Operator 대시보드 | `src/aegis/web/static/` | ✓ 교육·시연 용도 유지 |

---

## 2. 리팩토링 필요 (기존 코드 수정)

### 2.1 ATV 스키마 전면 교체 — 특허 Appendix A 매핑

```
SW BAND  (0..1879, 1880-D)
  0     .. 767   agent_state_embedding        768
  768   .. 1407  action_history               640
  1408  .. 1535  inter_agent_graph            128
  1536  .. 1599  memory_provenance            64
  1600  .. 1615  qom_scores                   16
  1616  .. 1647  resource_access_pattern      32
  1648  .. 1663  prompt_structure             16
  1664  .. 1671  aid_ats_scalars               8
  1672  .. 1683  encryption_metadata          12
  1684  .. 1747  output_content_fingerprint   64
  1748  .. 1779  tool_arg_inspection          32
  1780  .. 1795  action_blast_radius          16
  1796  .. 1807  output_channel_diversity     12
  1808  .. 1823  session_behavioral_drift     16
  1824  .. 1835  mcp_trust_signals            12
  1836  .. 1851  grounding_metrics            16
  1852  .. 1855  novelty_score                 4
  1856  .. 1863  human_oversight_state         8
  1864  .. 1879  cost_efficiency_metrics      16  ← 유일하게 현행 일치
HW BAND  (1880..2079, 200-D)  — T2는 zero-fill
  1880  .. 1911  memory_timing_histograms     32
  1912  .. 1935  aid_tag_transitions          24
  1936  .. 1951  atmu_anomaly                 16
  1952  .. 1967  dma_fanout                   16
  1968  .. 1983  thermal_ecc_drift            16
  1984  .. 1995  watchdog_signals             12
  1996  .. 2019  network_telemetry            24
  2020  .. 2035  gpu_accelerator_state        16
  2036  .. 2043  hypervisor_signals            8
  2044  .. 2059  hw_cost_attestation          16
  2060  .. 2079  linkage_consistency_features 20
```

**영향 파일**: `src/aegis/schema.py` (상수 30개로 확장), `src/aegis/atv/builder.py` (encoder 함수 30개로 확장 — 상당수는 T2에서 결정적 해시 또는 zero‑fill), 전체 테스트 재작성.

**`cost_efficiency_metrics` 16개 필드 (¶[0045])**:
```
s-1  input_token_count          current step
s-2  output_token_count         current step
s-3  reasoning_token_count      current step
s-4  cumulative_tokens          trace origin → now
s-5  cumulative_dollars         trace origin → now
s-6  tokens_per_successful_tool_invocation
s-7  tokens_per_plan_step_completed
s-8  tokens_per_byte_of_final_output
s-9  reasoning_to_action_ratio
s-10 cache_hit_rate             read‑only invocations
s-11 context_utilization_ratio
s-12 cost_delta_vs_role_baseline
s-13 budget_burn_rate
s-14 forecasted_cost_to_completion
s-15 task_progress_score        [0,1]
s-16 marginal_value_score       value/cost
```
현행 `CostEfficiency` 모델의 `exp_bytes_write/exp_dollars/…` 12필드는 **폐기** 후 s‑1..s‑16로 치환.

### 2.2 Firewall 350/360/370 분리 — 특허 ¶[0061]‑[0063]

- `step350_approval.py`: REQUIRE_APPROVAL 시 notification 채널 dispatch (Slack/email/webhook stub) + await (async task) + 타임아웃/거부 처리
- `step360_audit.py`: 결정 + 입력을 시리얼라이즈 → 서명 → Audit Log 추가 + (cost‑gate가 영향 주었다면) Cost Attestation Record 별도 append
- `step370_exec.py`: allow면 tool exec 허용, block이면 suppress + agent에 에러 리턴

현행 `api/evaluate.py`의 `_evaluate_impl`을 위 3개 step로 분해.

### 2.3 Burn‑in 개념 분리 — 현행 "burn‑in"을 "code_attestation"으로 리네이밍

현행 `src/aegis/attest/burn_in.py`의 L3(code)/L4(config)/L5(key) 해시는 **코드 측정치에 대한 attestation**이지 특허의 "Burn‑in (5‑layer 통계 baseline)"과 다르다.

| 이름 | 의미 | 파일 |
|---|---|---|
| (구) `burn_in.py` | 코드 해시 + pubkey 바인딩 | → `src/aegis/attest/code_attestation.py`로 개명 |
| (신) `burn_in_controller.py` | 5‑layer 통계 baseline + 4‑phase graduation | → `src/aegis/burnin/` 신규 패키지 (M11) |

`/attestation` endpoint는 코드 측정치 반환(현행 유지), 신규 `/burn-in-status` endpoint는 phase + layer별 baseline 요약 반환.

### 2.4 ATVHeader 필드 확장 — 특허 ¶[0049]

추가 필드:
- `parent_span_id: str | None`
- `node_id: str | None`
- `pod_id: str | None`
- `schema_version: str = "ATV-2080-v1"` (기존 `ats`는 alias 유지)
- `tier_profile: Literal["T2", "T3"] = "T2"`
- `cost_attestation_profile: Literal["software", "hardware", "both"] = "software"`
- `atv_hash: str` (SHA3‑256 of tensor) — 헤더 서명 시 포함

### 2.5 Cost Attestation Ledger 분리

현행: audit 레코드 안에 cost가 섞임 → Claim 3, 34 요구사항("distinct key slot for cost attestation", "selectively disclosable")과 불합치.

신규: `src/aegis/audit/cost_ledger.py` — audit DB와 별도 테이블/JSONL, 별도 Ed25519 키 슬롯(`./keys/ed25519_cost.pem`), 레코드 구조:
```
{ atv_commitment, cost_efficiency_metrics(16-D 직렬화), hw_cost_attestation(16-D, T2=zeros),
  divergence_metrics(3-D), trace_id, aid, tenant_id, ts_ns, signature, prev_hash, this_hash }
```
`/cost-attestation/{aid}` endpoint 신설.

---

## 3. 신규 구현 (M10‑M16)

### 3.1 M10 — Agent Transaction Management Unit (ATMU, 소프트웨어 2PC)

특허 Section 5A. SW 버전: SQLite 트랜잭션 + JSONL WAL.

- `src/aegis/atmu/intent_log.py`: Write‑Ahead Intent Log, 레코드 = ¶[0063B] 필드 세트
- `src/aegis/atmu/state_machine.py`: 상태 기계 {tentative → prepared → committed / aborted / rolled‑back / compensated / quarantined} per APPENDIX B
- 두 단계 커밋 흐름:
  1. `/evaluate` → firewall 결정 → WAL에 `tentative` 기록
  2. firewall=allow + cost OK + judge verdict=normal 시 `prepared` → `committed` 마커 추가
  3. `/approve` 경로: human OK면 `committed`, 거부/타임아웃이면 `aborted`
- 도구 실행 **후**의 post‑release 흐름:
  - `/tool-outcome` endpoint (¶[0063H‑1]): success/failure/timeout/partial/compensated + result hash + side‑effect receipt
  - compensating action 기록 (¶[0063H‑2]): 불가역 외부 효과(결제/이메일 등)에 대한 보상 거래 플랜 + 실행
- Checkpoint 기록 (¶[0063E]): 고‑blast 호출 전 working memory snapshot hash + policy version + capability manifest version

**테스트**: tentative→aborted, tentative→prepared→committed, prepared→rolled‑back, committed→compensated, 동시성 안전성 (100 concurrent intents).

### 3.2 M11 — 5‑layer 통계 Burn‑in + 4‑phase graduation

특허 Section 7. 이름 재활용하지 말고 **별개 모듈**.

```
src/aegis/burnin/
  layers.py        # L1 HW-invariant / L2 tenant / L3 topology / L4 role / L5 instance
  phases.py        # Observation → Shadow → Assisted → Production graduation gates
  controller.py    # 이벤트 기반 재‑캘리브레이션 (신규 agent/role/topology/tenant/firmware)
  composite.py     # composite anomaly score = weighted sum of layer-specific scores
```

MVP 수준 구현:
- L1: ATV HW band (T2=zeros) + 모델‑별 FLOPs/token 테이블 (JSON) — 연 단위 refresh
- L2: tenant별 SW band 통계 (mean/std per subfield), 분기 단위
- L3: 에이전트 delegation 그래프 (adjacency matrix hash), 주 단위
- L4: role 별 persona centroid = mean(agent_state_embedding) per role, 일 단위
- L5: instance‑level online micro‑adjustment, L4 envelope 초과 시 → L4 delta 재‑burn‑in 이벤트

Phase gates (¶[0075]):
- Observation → Shadow: 최소 1000개 ATV 수집
- Shadow → Assisted: TPR ≥ 0.95, FPR ≤ 0.02, precision ≥ 0.90 (shadow 판정 vs 라벨)
- Assisted → Production: 연속 N일 human override 비율 < 5%

데이터 소스: 현행 audit DB의 `payload_json`에서 ATV 벡터 복원 → in‑memory 통계. `/burnin-status` endpoint로 현재 phase + layer별 확신도 노출.

### 3.3 M12 — Dual‑band Cost + Divergence (software‑side)

특허 Section 12 + Claim 3, 26‑28.

T2는 HW 측정값이 없으므로 HW band는 zero. 그러나 **software‑estimated HW 프록시**를 만들어 divergence 수학은 테스트 가능:

- `src/aegis/cost/divergence.py`:
  - `token_to_flops_divergence(cost_metrics, model_hash)`: 소프트웨어 reported token count vs 모델별 expected FLOPs 룩업(`aegis/cost/model_flops.json`) 차이
  - `memory_cost_divergence`: context window size vs HW HBM bandwidth proxy (T2=0)
  - `dollar_cost_divergence`: 소프트웨어 $ vs FLOPs × $/FLOP coeff

T3 준비: hw_cost_attestation subfield 16‑D 필드명 전체 정의 (Appendix A j‑1..j‑16), populate는 T3에서.

### 3.4 M13 — sLLM attention‑attribution

특허 ¶[0066]. 현재 Haiku judge는 verdict + reason 반환 — attribution head는 없음.

SW 수준 구현: Haiku에게 system prompt로 "분류 직후, 30개 subfield 각각에 대해 0..1 contribution score도 같이 JSON으로 반환"를 요구. `src/aegis/judge/haiku.py` `evaluate()` 반환형 확장:
```python
@dataclass
class JudgeVerdict:
    decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
    confidence: float
    reason: str
    subfield_attribution: dict[str, float]  # 신규 — 30개 subfield → 0..1
```

대시보드 표시: Theater의 기존 5‑firewall‑check pipeline 아래에 subfield 기여도 bar chart 추가.

### 3.5 M14 — AID 인가 테이블 (software emulation of AID tag comparator)

특허 5B. T3는 HW tag comparator가 하지만, T2는 software 미들웨어로 흉내:
- `policies/aid_region.json`: `{ aid → { allowed_tools: […], allowed_paths: […], allowed_tenants: […] }}`
- `src/aegis/firewall/step315_aid_auth.py`: step 310/320 사이에 삽입. 호출한 aid가 tool/경로에 대해 allowed가 아니면 즉시 BLOCK + `aid_tag_transitions` subfield의 violation counter 증가.
- Circuit breaker mode: specific aid가 N회 violation 시 `quarantined` 상태로 이동 (ATMU 연동), 후속 `/evaluate` 모두 BLOCK. release는 `/admin/aid/release` 서명된 엔드포인트로만.

### 3.6 M15 — 암호화 ATV 저널 + forensic replay

특허 13B + Claim 21.

- `src/aegis/audit/encrypted_journal.py`: 기존 JSONL을 AEAD (AES‑GCM 또는 ChaCha20‑Poly1305)로 wrap. 키는 `./keys/journal_data_key.bin` (env‑설정 가능), nonce 매 레코드마다 랜덤.
- Power‑fail 시뮬레이션: 프로세스 강제 종료 후 재시작 시 journal을 스캔, torn write detection(auth tag 실패 시 레코드 폐기), 마지막 `committed` 마커까지 replay.
- `src/aegis/audit/replay.py`: WAL 스캔 → ATMU 상태 재생성 → 결정론 firewall 재평가 → 기록된 verdict와 대조 → 불일치 시 tampering/drift 플래그. replay attestation 서명 후 반환.

### 3.7 M16 — HAM‑lite (stretch, software only)

특허 13A. SW 수준에서 L3/L4만 모사:
- L3: in‑process dict (hot objects), size‑bounded
- L4: 디스크(기존 sqlite)에 encrypted HAM table
- L1/L2 (HBM/CXL): stub API로 자리만 남김
- Operations (¶[0102C]): `memory / recall / context / forget / summarize / ground` REST endpoints
- 각 객체에 aid/tenant/ts/digest 바인딩

MVP 우선순위 낮음 — 실제 에이전트가 쓸 need가 있어야 가치 드러남. 후순위.

---

## 4. 본 MVP 범위 밖 (T3 하드웨어 의존)

| 항목 | 특허 참조 |
|---|---|
| CSD 통합 (NVMe controller firmware, FPGA/AIE 분류기) | §9, Claim 21 |
| CXL 직접 체크포인팅, zero‑copy restore | §9A, Claim 22 |
| HW tag comparator at memory controller | §5B ¶[0063K], Claim 22 |
| Per‑AID HW 리소스 카운터 (FLOPs/HBM/power/energy) | §3.3 hw_cost_attestation ¶[0047], Claim 3 |
| On‑die 열/전력 센서, ECC drift counter | §3.3 thermal_ecc_drift |
| TEE‑sealed signing key (Nitro Enclave / TDX / SEV‑SNP) | §4 ¶[0050], Claim 1 (HW‑rooted) |
| ML‑DSA (post‑quantum) 이중 서명 | Claim 25 |
| On‑device sLLM (FPGA bitstream, 4‑bit quantized) | §6 ¶[0067], Claim 10 |
| In‑storage vector similarity | §13 |

→ **API 모양만 맞추고** (`tier_profile="T2"`, hw band=zeros, `cost_attestation_profile="software"`) T3 전환 시 구현체만 교체할 수 있게.

---

## 5. 마이그레이션 전략

**Break‑or‑migrate 결정**: 현행 코드는 프로덕션 사용자가 없으므로 **스키마 브레이킹 허용**. 단:
- 기존 audit 체인 (data/audit.sqlite)은 `ATV-2080-v0‑legacy`로 스키마 버전 마킹 후 read‑only 유지
- 새 체인은 `ATV-2080-v1`로 시작
- `/audit/{aid}` API는 두 버전 혼재를 읽을 수 있도록 버전 디스패치

**순서**:
1. **M8** 스키마 교체 + encoder 30개 구현 + 테스트 리라이트 (가장 크고 위험한 변경 → 먼저)
2. **M9** firewall 350/360/370 분리 (작고 국소)
3. **M10** ATMU (M8·M9 선행 필요)
4. **M11** Burn‑in controller (M8 선행)
5. **M12** cost divergence + ledger (M8 선행)
6. **M13** attention attribution (M8 선행)
7. **M14** AID auth (M10 선행)
8. **M15** 암호화 저널 + replay (M10 선행)
9. **M16** HAM‑lite (선택)

각 milestone: PR 단위 커밋, pytest/ruff/mypy green 유지, Theater 해당 시나리오 업데이트 (있다면).

---

## 6. 개정된 레포 구조

```
src/aegis/
├── schema.py                       ATV-2080-v1 30개 subfield 상수 + ATVHeader 확장
├── config.py
├── main.py
├── atv/
│   ├── builder.py                  30개 encoder (T2에선 다수 결정적 해시 or zero-fill)
│   ├── encoders/                   ← 신규; subfield 별 encoder 모듈
│   │   ├── agent_state_embedding.py
│   │   ├── action_history.py
│   │   ├── ...                     (30개)
│   │   └── hw_zero.py              T2 HW band 일괄 zero-fill
│   └── embeddings.py               기존
├── firewall/
│   ├── core.py
│   ├── step310_args.py
│   ├── step315_aid_auth.py         ← 신규 M14
│   ├── step320_blast.py
│   ├── step330_human.py
│   ├── step335_cost.py
│   ├── step340_policy.py
│   ├── step350_approval.py         ← 신규 M9 (분리)
│   ├── step360_audit.py            ← 신규 M9
│   └── step370_exec.py             ← 신규 M9
├── atmu/                           ← 신규 M10
│   ├── intent_log.py
│   ├── state_machine.py
│   ├── checkpoint.py
│   └── compensating.py
├── burnin/                         ← 신규 M11 (통계 baseline, 코드 해시 아님)
│   ├── layers.py                   L1..L5
│   ├── phases.py                   Observation/Shadow/Assisted/Production
│   ├── controller.py               이벤트 기반 재-캘리브레이션
│   └── composite.py                composite anomaly score
├── cost/                           ← 신규 M12
│   ├── metrics.py                  s-1..s-16
│   ├── divergence.py               3 divergence metrics
│   ├── model_flops.json            모델별 FLOPs/token 테이블
│   └── ledger.py                   Cost Attestation Ledger
├── attest/
│   ├── code_attestation.py         ← 기존 burn_in.py 개명 (L3/L4/L5 코드 해시)
│   └── __init__.py
├── sign/
│   ├── ed25519.py
│   ├── merkle.py
│   └── pq_stub.py                  ← ML-DSA stub (Claim 25)
├── audit/
│   ├── sqlite_store.py
│   ├── jsonl_store.py
│   ├── encrypted_journal.py        ← 신규 M15
│   ├── replay.py                   ← 신규 M15
│   └── cost_ledger.py              ← 신규 M12 (cost/ledger.py alias)
├── judge/
│   ├── base.py
│   ├── haiku.py                    attribution head 반환 확장 (M13)
│   └── dummy.py
├── ham/                            ← 신규 M16 (stretch)
│   ├── levels.py
│   ├── interface.py                memory/recall/context/forget/summarize/ground
│   └── encryption.py
├── api/
│   ├── evaluate.py                 slim: build→ATMU tentative→firewall→ATMU commit
│   ├── approve.py
│   ├── audit_query.py
│   ├── attestation.py              코드 attestation (현행 유지)
│   ├── burnin_status.py            ← 신규 M11: phase/layer 상태
│   ├── cost_attestation.py         ← 신규 M12: /cost-attestation/{aid}
│   ├── tool_outcome.py             ← 신규 M10: post-release 결과 기록
│   ├── admin_aid.py                ← 신규 M14: circuit breaker release
│   └── ham.py                      ← 신규 M16 (stretch)
├── web/static/                     기존 대시보드 유지 + subfield attribution 패널 추가
policies/
├── default.json                    기존 allow/deny 룰
├── aid_region.json                 ← 신규 M14: AID→region authorization
├── tenant_budgets.json             기존 inline 테이블을 JSON으로
└── claim_map.json                  ← 신규: milestone → 특허 claim 번호 매핑
tests/
├── unit/… (기존)
├── atmu/                           ← 신규 M10
├── burnin/                         ← 신규 M11
├── cost/                           ← 신규 M12
└── integration/
    ├── test_firewall_e2e.py        기존
    ├── test_atmu_2pc.py            ← 신규 M10
    ├── test_cost_divergence.py     ← 신규 M12
    └── test_replay.py              ← 신규 M15
tools/
└── …                               기존 (aegis_hook.py, aegis_safety.py, setup_macmini.sh, install_hook.py)
demo/
└── …                               기존
```

---

## 7. 특허 Claim 매핑

| Claim # | 요약 | 현행 커버리지 | 담당 Milestone |
|---|---|---|---|
| 1 | ATV assembler + TEE‑signed commitment + tamper‑evident audit log | 부분 (TEE 없음, SW signing) | M8 (schema), 기존 (서명/audit) |
| 2 | Pre‑commit Action Firewall + ATMU + forecasted‑cost gating | 부분 (firewall O, ATMU X, forecasted cost X) | M9, M10, M12 |
| 3 | Dual‑band cost attestation (SW + HW) + divergence + TEE‑signed cost ledger | 부분 (SW만, ledger 별도 아님) | M12 |
| 4 | 5‑layer factorized Burn‑in + event‑driven partial recalibration | 없음 (현행 burn_in은 코드 해시) | M11 |
| 5 | Integrated system (claim 1 + 2 + 3 + 4 공유 ATV) | 부분 | M8‑M12 모두 |
| 6 | HW band에 atmu_anomaly/watchdog/hw_cost_attestation 서브필드 | 없음 | M8 (스키마), T2는 zero‑fill |
| 7 | SW band에 action_blast_radius/output_content_fingerprint/session_behavioral_drift/novelty_score 서브필드 | 없음 | M8 |
| 8 | 0.1‑1B param sLLM 3‑class 분류기 | 범용 Claude로 대체 | M13 (attribution만) |
| 9 | 정확히 2080‑D, cost_efficiency_metrics 1864..1879, hw_cost_attestation 2044..2059, linkage_consistency 2060..2079 | 부분 (크기만 맞음) | M8 |
| 10 | FPGA/AIE 배포 | T3 범위 밖 | — |
| 11 | 결정론 sLLM inference (bit‑exact on FPGA/AIE) | 부분 (temperature=0 있음) | M13 |
| 12 | linkage_consistency_features subfield | 없음 | M8 (스키마), M12 (computation) |
| 13 | 4‑phase Burn‑in (Observation/Shadow/Assisted/Production) + threshold gates | 없음 | M11 |
| 14 | 5 independently recalibrated layers | 없음 | M11 |
| 15 | Firewall submodules: loop detection / dedup / forecasted‑cost gating | 없음 | M9 (확장) + M12 |
| 16 | Method claim (Firewall + ATMU + 서명) | 부분 | M9/M10 |
| 17 | SHA3‑256 + Ed25519/ML‑DSA | ✓ (SHA3+Ed25519) | 기존 (ML‑DSA는 Claim 25) |
| 18 | Loop detection & abort | 없음 | M9 확장 |
| 19 | Method claim: Burn‑in factorization + partial recalibration | 없음 | M11 |
| 20 | Event types that affect specific layers | 없음 | M11 |
| 21 | CSD 장치 자체 | T3 범위 밖 | — |
| 22 | CSD + AID enforcement at memory controller | T3 범위 밖 | — |
| 23 | Non‑transitory CRM | ✓ (Docker 이미지) | 기존 |
| 24 | 단일 스키마 T2/T3 uniformity | ✓ (hw band zero‑fill) | M8 |
| 25 | ML‑DSA (post‑quantum) 이중 서명 | 없음 | `sign/pq_stub.py` (Claim 25 only; 우선순위 낮음) |
| 26 | cost_delta + 3 divergence metrics | 없음 | M12 |
| 27 | Shadow‑phase baseline 기반 cost divergence + 독립 escalation | 없음 | M12 + M11 |
| 28 | 방법 claim: cost divergence 탐지 | 없음 | M12 |
| 29 | Selective disclosure cost attestation | 없음 | M12 |
| 30 | Cost Attestation Record의 ATV commitment 바인딩 | 없음 | M12 |
| 31 | CSD 방법 claim | T3 범위 밖 | — |
| 32 | Cost‑based SLA enforcement module | 없음 | M12 확장 |
| 33 | Forecasted cost 구성 (progress + role baseline + plan state) | 없음 | M12 |
| 34 | Cost signing key slot이 telemetry key와 구분 | 없음 | M12 (별도 키 파일) |
| 35‑39 | Means‑plus‑function 매핑 (기능 claim 대응) | N/A | 구조 claim 구현으로 자동 |
| 40 | ZK range proof for cost dimensions | 없음 | stretch (M12 옵션) |

---

## 8. 수정된 Definition of Done

기존 Milestone 1‑7 DoD는 유지(완료 상태). 추가 DoD:

- `docker compose up` → `/healthz` 녹색 + burn‑in phase 표시
- `POST /evaluate` → 특허 step 310‑370 순차 실행 + ATMU tentative→committed 기록 + cost ledger에 Cost Attestation Record 1건
- `GET /attestation` → 코드 attestation (기존)
- `GET /burnin-status` → 현재 phase + 5개 layer별 baseline 존재 여부 + 샘플 수
- `GET /cost-attestation/{aid}` → 선택적 공개 가능한 cost 레코드 목록
- `POST /tool-outcome` → post‑release 결과 기록 + compensating action 트리거 가능
- `pytest --cov=aegis` 커버리지 ≥ 70% (기존 95%에서 스키마 리팩토링으로 일시 하락 허용 → 복구)
- Claim 1, 2, 3, 4, 5, 15, 17, 24의 ASCII 테스트 가능 요소가 모두 엔드‑투‑엔드 자동 검증됨

---

## 9. 타임라인 (7‑일 단위, 하루 3‑5시간 가정)

| Milestone | 일수 | 주요 산출물 |
|---|---|---|
| M8 schema | 3 | 30 subfield encoder + ATVHeader 확장 + 전체 테스트 재작성 |
| M9 firewall 350/360/370 분리 | 1 | 3개 새 step 파일 + e2e 테스트 통과 |
| M10 ATMU | 3 | intent_log/state_machine/checkpoint + 2PC e2e + compensating |
| M11 Burn‑in | 4 | 5 layers + 4 phases + graduation gates + `/burnin-status` |
| M12 Cost ledger + divergence | 3 | cost_ledger + 3 divergence + 별도 키 슬롯 + selective disclosure 스텁 |
| M13 attribution | 1 | Haiku system prompt 확장 + 30‑bar 차트 |
| M14 AID auth | 2 | step315 + aid_region.json + 회로차단 + `/admin/aid/release` |
| M15 암호화 저널 + replay | 3 | AEAD wrapper + replay engine + tampering 탐지 |
| M16 HAM‑lite | 2 | (stretch) L3/L4 software emulation |
| **합계** | **22일** | |

---

## 10. 향후 변경 관리

- 특허 문구가 개정되면 이 문서 상단에 **변경 요약** 섹션 추가 + 영향받는 Milestone 표시.
- `policies/claim_map.json`을 SSOT로 삼아, 각 claim 번호에 대해 "covered / partial / out‑of‑scope" 상태를 코드로 기계 검증 (M11 완료 이후 `tools/claim_coverage.py` 스크립트 추가 가능).
- 특허 outputs (Cost Attestation Record JSON 스키마, WAL 레코드 스키마) 변경은 `schema_version` 필드로 버저닝.

---

**문서 끝**. 본 v2 계획은 기존 MVP 자산(116 테스트, Theater, 대시보드, hook, setup)을 유지한 채 특허 v7.10과의 정합성을 확보하기 위한 증분 작업 명세서다. 전면 재작성이 아니라 **Milestone 8‑16을 순차 추가**하는 방식이 안전하고, 각 Milestone은 독립적으로 릴리스 가능한 단위로 설계되었다.
