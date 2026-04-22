# PLAN v3 — AegisData T3 (하드웨어 티어) 설계

**참조 명세**: `AegisData_US_Provisional_Patent_ATV_v7_10.docx` (Draft 2026-04-21)
**선행 문서**: [`PLAN.md`](PLAN.md) (M1–M7 원본 MVP), [`PLAN_v2.md`](PLAN_v2.md) (M8–M16 patent-aligned T2)
**대상 티어**: T3 — 하드웨어 의존 (TEE, CSD, FPGA/AIE, HW perf counter)
**전제**: 현재 `main` 브랜치는 PLAN_v2 M8–M16 완료 상태 (commit `5a7b7d3`). 326 tests pass · ruff clean · mypy strict (61 source files). Docker 컨테이너 + 대시보드 + 데모 자료까지 풀 surface 가동 중.

이 문서는 **T2 software 구현체를 그대로 두고 hardware 구현체를 추가**하는 증분 작업 명세서다. 외부 contract (스키마, endpoint, 서명 포맷) 는 **변경하지 않는다** — 이미 PLAN_v2가 T3 전환을 염두에 두고 placeholder를 박아뒀다 (`tier_profile="T2"|"T3"`, `cost_attestation_profile="software"|"hardware"|"both"`, HW band 200-D zero-fill, `schema_version`). T3는 이 placeholder들을 채우는 substitution 작업이다.

---

## 0. T2 → T3 한 페이지 diff

| 영역 | T2 (현재) | T3 (이 문서) | 무엇이 바뀌나 |
|---|---|---|---|
| ATVHeader.tier_profile | `"T2"` | `"T3"` | 1글자 |
| ATVHeader.cost_attestation_profile | `"software"` | `"both"` (또는 `"hardware"`) | 라벨 |
| HW band (1880..2079, 200-D) | 전부 0 | 11개 subfield 실측치 | **encoder 11개 신규** |
| Code attestation (`/attestation`) | source hash + Ed25519 | TEE quote (TDX/SEV-SNP MRENCLAVE 동등) | 측정 root 교체 |
| Signing key 보관 | `keys/*.pem` 평문 파일 | TEE-sealed (enclave 외부로 절대 안 나감) | 키 관리만 교체 |
| sLLM judge | Anthropic Haiku 4.5 (network) | 0.1–1B 파인튜닝 모델, FPGA/AIE 4-bit, bit-exact deterministic | 실리콘 추론 |
| AID 인가 | step315 software middleware | HW tag comparator at memory controller | 회로 enforcement |
| HAM L1 (register-cache) | OrderedDict in-process | HBM register file (real L1) | 캐시 backing |
| HAM L2 (NVMe-tier) | (T2에서는 미구현) | CSD-DRAM (Solidigm/Samsung NVMe-CSD) | 신규 backing |
| 서명 알고리즘 | Ed25519 단일 | Ed25519 + ML-DSA 이중 (Claim 25) | 신규 cosign |
| 체크포인트 (M10 ATMU) | host RAM | CXL 직접 체크포인팅, zero-copy restore | 카피 제거 |
| `linkage_consistency_features` (2060..2079) | 0 | SW↔HW band cross-tampering vector | 신규 computation |

**핵심**: 외부 사용자가 보는 JSON 모양·endpoint·서명 검증 절차는 **변하지 않는다**. `tier_profile` 필드와 16개의 0 → 실수 변화만 보인다. 이게 patent Claim 24의 "단일 스키마 T2/T3 uniformity" 의 의도다.

---

## 1. T2가 미리 박아둔 placeholder 목록

`src/aegis/schema.py` 가 이미 T3를 알고 있다. 다음 11개 subfield는 **T2에서 zero-fill** 이지만 **이름·인덱스·차원이 확정**돼 있어서 T3 encoder만 새로 쓰면 된다:

| Slice | 차원 | T2 source | T3 source |
|---|---|---|---|
| `memory_timing_histograms` 1880..1911 | 32 | 0 | DRAM 컨트롤러 perf counters (`/sys/devices/uncore_imc/`), EDAC stats |
| `aid_tag_transitions` 1912..1935 | 24 | 0 | HW tag comparator transitions count + violation flags |
| `atmu_anomaly` 1936..1951 | 16 | 0 | TEE-bound 2PC violation counter (intent_log monitor에서 export) |
| `dma_fanout` 1952..1967 | 16 | 0 | IOMMU stats (Intel VT-d, AMD IOMMU), CXL.mem traffic |
| `thermal_ecc_drift` 1968..1983 | 16 | 0 | On-die thermal sensors + EDAC ECC counters (`/sys/devices/system/edac/`) |
| `watchdog_signals` 1984..1995 | 12 | 0 | TPM watchdog, BMC heartbeat, TEE quote freshness |
| `network_telemetry` 1996..2019 | 24 | 0 | NIC perf counters, eBPF-tracked SmartNIC offload statistics |
| `gpu_accelerator_state` 2020..2035 | 16 | 0 | NVIDIA NVML/DCGM/CUPTI for H100/H200, AMD ROCm, Intel HBI |
| `hypervisor_signals` 2036..2043 | 8 | 0 | TDX QGS / SEV-SNP attestation report scalars |
| `hw_cost_attestation` 2044..2059 | 16 | 0 | RAPL / MSR perf counters / DCGM power tracking |
| `linkage_consistency_features` 2060..2079 | 20 | 0 | SW↔HW band cross-band tampering vector (M25) |

**합계 200-D**. 모두 SLICE_HW_BAND (1880..2080) 안에 contiguous.

추가 placeholder:
- `ATVHeader.tier_profile: Literal["T2", "T3"]` ← 이미 `"T3"` 허용
- `ATVHeader.cost_attestation_profile: Literal["software", "hardware", "both"]` ← 이미 `"hardware"` / `"both"` 허용
- `ATVHeader.node_id`, `pod_id` ← T3에서 TEE 인스턴스 고유 ID (TDX MRENCLAVE 또는 SEV-SNP measurement) 가 들어감
- `ATVHeader.model_hash` ← T3에서 FPGA/AIE에 burn된 sLLM bitstream hash
- `aegis.sign.pq_stub` ← M18에서 진짜 ML-DSA로 교체

---

## 2. 신규 Milestone (M17-M26)

### Phase A — TEE software path (cloud-available, $)

> 클라우드에서 바로 가능. Azure DCsv5/DCdsv3 (Intel TDX), GCP C3 Confidential (Intel TDX), AWS R7iz (AMD SEV-SNP) 중 택일. 한 시간에 $1–3 수준의 인스턴스로 검증 가능.

#### M17 — TEE attestation (Intel TDX 또는 AMD SEV-SNP)

**특허 reference**: §4 ¶[0050], Claim 1 (HW-rooted commitment), Claim 24 (T2/T3 schema uniformity)

T2의 `aegis.attest.code_attestation`는 source hash를 Ed25519로 서명한다. T3는 이걸 **TEE quote** 으로 교체한다.

- 신규 모듈 `aegis.attest.tee_quote` — TDX 환경: `/dev/tdx_guest` ioctl로 quote 생성 (Intel `tdx-attest-rs` 패턴), SEV-SNP 환경: `/dev/sev-guest` 로 attestation report 생성
- 새 endpoint `GET /attestation/tee-quote` — quote (raw bytes) + report data (32 bytes 내부 hash, T2 burn_in_id를 박음) + collateral (TCB info, QE identity)
- `GET /attestation` 은 quote의 `report_data` 안에 T2 burn_in_id를 박아서 반환 → T2 attestation의 root가 TEE measurement
- `cost_attestation_profile` 자동 변경 ("software" → "both")
- `ATVHeader.node_id` 자동 채움 (TDX MRTD 또는 SEV-SNP launch measurement)

**DoD**:
- Azure DCsv5 또는 GCP C3 Confidential 인스턴스에서 `docker compose up -d` → `/attestation/tee-quote` 가 valid quote 반환
- Intel SGX/TDX TCB validator (Intel `dcap-quote-verification`) 또는 AMD `snpguest` 로 quote 검증 → pass
- T2 모드 (TEE 부재 시) fallback: `/attestation` 는 기존대로, `/attestation/tee-quote` 는 503 + reason

**예상 작업량**: 4일

---

#### M18 — ML-DSA post-quantum dual-signing (Claim 25)

**특허 reference**: Claim 17, 25. 우선순위 낮으나 patent에 명시.

T2의 `sign/pq_stub.py` 가 자리만 있다. T3는 진짜 ML-DSA-65 (CRYSTALS-Dilithium L3) 로 dual-sign.

- Dependency: `liboqs` + `oqs-python` (Open Quantum Safe). 또는 Python `cryptography` 가 ML-DSA 추가될 때까지 `pyOQS`.
- 모든 signed record (`audit/sqlite_store`, `cost/ledger`, `attest/burn_in`) 가 **두 서명을 모두 포함**:
  - `signature_ed25519: hex (64 bytes)` ← 기존
  - `signature_ml_dsa: hex (~3293 bytes for ML-DSA-65)` ← 신규
- Verifier는 둘 중 **하나라도 fail이면 record reject** (defense-in-depth)
- 키 자동 생성 경로: `keys/ml_dsa_65.pem` (private), `keys/ml_dsa_65.pub` (public). 4 번째 telemetry+cost 키 쌍이 추가됨
- T2/T3 양쪽에서 활성 (TEE 없이도 가능). 단 키 사이즈가 커서 audit DB 행 사이즈가 ~3.5KB → ~7KB로 증가

**DoD**:
- 모든 signed record가 두 서명을 가진다 (`audit/{aid}` 응답에 `signature_ml_dsa` 노출)
- `verify_chain()` 이 두 서명 모두 검증
- 326 tests + 신규 ML-DSA 단위 테스트 ~10개 통과
- Audit DB 사이즈 증가 측정값 README에 기록

**예상 작업량**: 3일 (oqs-python 패키징이 빠르면 2일)

---

#### M19 — HW perf counter readout for cost attestation

**특허 reference**: §3.3 hw_cost_attestation ¶[0047], Claims 3, 6 (HW band 부분), 26-30 (HW-side divergence)

`hw_cost_attestation` (2044..2059, 16-D) 에 진짜 HW 측정값을 채운다. M12의 SW divergence를 **SW vs HW** divergence로 확장.

- 신규 `aegis.cost.hw_counters`:
  - **CPU power**: Linux RAPL via `/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj`
  - **DRAM power**: `intel-rapl:0/intel-rapl:0:0/energy_uj`
  - **DRAM bandwidth**: uncore IMC counters via `perf stat -e uncore_imc/cas_count_read/`
  - **GPU power + utilization**: NVML / DCGM (`nvmlDeviceGetPowerUsage`, `nvmlDeviceGetUtilizationRates`)
  - **Per-process FLOPs**: cgroup v2 + DCGM `DCGM_FI_PROF_PIPE_FP32_ACTIVE`
- `aegis.atv.encoders.hw_cost_attestation` 가 16-D 벡터를 채움 (현재 zero-fill encoder를 substitute)
- `aegis.cost.divergence` 에 `compute_sw_vs_hw_divergence()` 추가:
  - SW가 예측한 dollars vs HW counter 기반 measured dollars
  - SW가 예측한 tokens vs HW counter 기반 implied compute (FLOPS / token-FLOPS-table)
  - Claim 27 의 "SW↔HW divergence" 가 이걸 정확히 가리킴
- `cost_attestation_profile` = `"both"`

**DoD**:
- `/cost-attestation/{aid}` 응답에 `sw_cost_metrics` + `hw_cost_metrics` 둘 다 포함
- HW counter 미가용 환경 (containerd/docker 권한 부족) → graceful degrade to "software" only with warning
- Single-call latency 영향 < 5ms (perf counter readout은 ~1ms)

**예상 작업량**: 5일 (NVML/DCGM 셋업 + HW에 따른 분기)

---

### Phase B — In-storage / accelerator path ($$, 파트너십 필요)

> 실제 NVMe-CSD 또는 FPGA dev kit이 필요. Solidigm/Samsung CSD eval program, Xilinx Versal AI Edge VEK280 ($2K) 정도. 클라우드만으로는 안 됨.

#### M20 — FPGA/AIE 결정론 sLLM judge (Claims 8, 10, 11)

**특허 reference**: §6 ¶[0067], Claims 8 (0.1-1B param sLLM), 10 (FPGA/AIE deployment), 11 (bit-exact deterministic)

T2/M13는 Claude Haiku를 호출 + attribution head를 system prompt로 강제. T3는 이걸 실리콘에 박힌 작은 모델로 교체.

- **모델 선택**: Llama-3.2-1B-Instruct 또는 Phi-3-mini (3.8B → 4-bit quant → ~2GB) 중 정책-fine-tuned 변형. 출력 head 3개:
  - verdict (3-class: ALLOW/BLOCK/REQUIRE_APPROVAL)
  - confidence (scalar 0..1)
  - attribution (30-D vector → 30 subfield별 contribution)
- **하드웨어**: Xilinx Versal AI Edge VEK280 (또는 AMD MI300X for the larger Phi variant)
- **런타임**: Vitis AI 5.0 또는 Brevitas + ONNX Runtime
- 신규 `aegis.judge.fpga_aie` — FPGA bitstream으로 send + receive 31-D output
- **결정론**: temperature=0, deterministic kernel (no atomics non-determinism on AIE), 같은 ATV → 같은 output (bit-exact). Property-based test로 1000 iter 동일 결과 검증
- `ATVHeader.model_hash` ← FPGA bitstream의 SHA3-256
- T2 fallback 유지: FPGA 미가용 시 자동으로 Haiku로 떨어짐

**DoD**:
- VEK280 에서 sLLM 추론 < 50ms (Haiku의 180ms보다 빠름이 목표)
- 같은 입력으로 1000회 호출 → 100% bit-exact 일치
- 30-bar attribution 차트가 대시보드에서 (T2의 placeholder 그대로) 동작
- `/attestation` 응답에 `model_hash` 노출 → bitstream tampering 탐지 가능

**예상 작업량**: 14일 (모델 fine-tuning 5일 + Vitis AI 빌드 5일 + 통합 테스트 4일)
**리스크**: Vitis AI 5.0의 LLM 지원이 미성숙. Plan B는 Hailo-15 또는 Apple Neural Engine via CoreML.

---

#### M21 — HW tag comparator at memory controller (Claim 22)

**특허 reference**: §5B ¶[0063K], Claim 22

T2/M14의 `step315_aid_auth.py` 는 software middleware. T3는 같은 enforcement를 **memory controller에서** 수행 → tool 실행 전에 메모리 접근 자체가 막힘.

- 두 가지 substrate 옵션:
  1. **Intel VT-d / AMD IOMMU** with PASID (Process Address Space ID) — 각 AID가 자기 PASID 도메인을 가짐. Tool process는 자기 도메인 내 메모리만 DMA 가능
  2. **CXL 3.0 memory controller** with tag-based access control — 더 깔끔하지만 hardware 가용성 제한
- 신규 `aegis.firewall.hw_tag_comparator`:
  - Boot 시 `aid_region.json` 정책을 IOMMU page table로 컴파일
  - `aid_tag_transitions` (1912..1935, 24-D) 에 transition 횟수 + violation 플래그 채움
  - Violation 발생 시 step315의 `_breaker.record_violation()` 자동 호출 (M14 회로차단과 backward-compatible)
- T2 fallback 유지: VT-d/CXL3 미가용 시 software middleware로 떨어짐

**DoD**:
- VT-d/IOMMU 환경에서 disallowed memory access → IOMMU page fault → step315 violation 자동 기록
- 같은 violation count로 자동 quarantine 발생 (M14 동작과 동일)
- 단위 테스트는 IOMMU 가용 호스트에서만 실행 (CI는 software fallback)

**예상 작업량**: 7일
**리스크**: VT-d 프로그래밍은 root 권한 + 호스트 OS 협조 필요. 컨테이너 안에서는 어려움 → bare-metal deployment 전제.

---

#### M22 — CSD (Computational Storage Device) integration

**특허 reference**: §9, Claims 21, 31

HAM L2 (NVMe-tier) 를 진짜 NVMe-CSD에 in-storage offload. M16의 L3+L4 SQLite는 그대로 유지하면서 L2를 CSD로 추가.

- 하드웨어: Solidigm Computational SSD (CSD-2000) 또는 Samsung SmartSSD with NVMe-oF Computational Storage protocol
- 신규 모듈:
  - `aegis.ham.csd_l2` — `set_csd(csd_handle)` 으로 L2 backing 등록. memory()/recall() 가 L2 hit 시 zero-copy
  - `aegis.ham.in_storage_similarity` — CSD 안에서 cosine similarity 직접 수행 (no host CPU pull). HAM recall의 tag filter를 vector similarity로 확장
- T2 fallback: CSD 미가용 시 L2 layer 비활성, L3+L4 SQLite로만 동작 (M16 그대로)

**DoD**:
- Solidigm CSD eval kit에서 100K HAM items 저장 → recall 평균 latency < 5ms (T2 SQLite는 ~15ms)
- In-storage similarity: top-10 most similar items in < 10ms over 1M-item store
- T2 fallback 모드와 동일한 외부 contract (HAM endpoints 그대로)

**예상 작업량**: 21일
**리스크**: CSD eval program 접근권 필수 + 벤더별 SDK 학습. 가장 hardware-dependent한 milestone.

---

### Phase C — Cross-cutting hardening ($, T3-only 가치)

#### M23 — Per-AID HW resource counters

**특허 reference**: §3.3 hw_cost_attestation ¶[0047], Claim 3 (full HW band)

M19가 시스템 전체 HW counter를 본다면, M23은 **per-AID 분리**해서 본다. 같은 호스트에 여러 agent가 같이 돌 때 누가 얼마나 썼는지 분리.

- Linux cgroup v2 per-process tracking (`memory.current`, `cpu.stat`, `io.stat`)
- DCGM `nvidia-smi pmon` 또는 MIG (Multi-Instance GPU) per-instance counters
- 신규 encoder들 채움:
  - `atmu_anomaly` (1936..1951) ← per-AID 2PC violation rate
  - `dma_fanout` (1952..1967) ← per-AID DMA transfer count
  - `gpu_accelerator_state` (2020..2035) ← per-AID GPU mem + util
- 새 endpoint `GET /hw-counters/{aid}` — per-AID HW counter snapshot

**DoD**:
- 두 agent 동시 실행 → `GET /hw-counters/{aid1}` 와 `/{aid2}` 가 서로 다른 값 반환
- 합산이 시스템 전체 (`/hw-counters/_all`) 와 일치 (오차 < 5%)

**예상 작업량**: 5일

---

#### M24 — TEE-bound key sealing

**특허 reference**: §4 ¶[0050], Claims 1, 17, 25

T2는 4개 키 (`telemetry/cost/journal-AES/HAM-AES`) 가 `keys/*.pem` 파일로 평문 저장. T3는 이걸 TEE 안에서 생성·사용·sealing.

- TEE 환경:
  - **Intel TDX**: Migration TD with sealed key blob + `tdx_attest_get_quote()` 로 외부 검증
  - **AMD SEV-SNP**: VMPL-bound key with `snp-guest` derive
- 신규 `aegis.sign.tee_sealed`:
  - `seal_key(key_bytes) -> sealed_blob`
  - `unseal_key(sealed_blob) -> key_bytes` (현재 enclave에서만 동작)
  - 부팅 시 sealed blob을 디스크에서 로드 → enclave에서 unseal → 메모리 사용 → 종료 시 zero
- T2 키 파일은 first-boot에 자동 sealing 후 삭제 (one-way migration)

**DoD**:
- `keys/*.pem` 평문 파일이 디스크에 절대 존재하지 않음 (only `keys/*.sealed`)
- TEE attestation quote → external verifier 가 키 가 진짜 enclave 안에 있다고 확인 가능
- Migration: T2 → T3 첫 부팅 시 자동 sealing + 평문 키 secure-wipe

**예상 작업량**: 5일

---

#### M25 — Linkage consistency features

**특허 reference**: Claim 12, 27 (SW↔HW divergence as a vector signal)

`linkage_consistency_features` (2060..2079, 20-D) 를 채움. 지금까지 0이었던 마지막 subfield. SW band와 HW band의 cross-tampering 신호를 벡터로.

- 20-D 구성안:
  - 0..3: SW vs HW cost dollar divergence (mean, max, std, slope)
  - 4..7: SW vs HW token-to-FLOPs divergence
  - 8..11: SW agent_state_embedding vs HW gpu_accelerator_state correlation
  - 12..15: ATMU intent state vs HW atmu_anomaly correlation
  - 16..19: 시간축 lag features (SW와 HW measurement timestamp 차이)
- 새 escalation 룰: `linkage_consistency_features` magnitude > threshold → `EscalationDecision(should_escalate=True, reason="sw_hw_drift")`
- 이 escalation은 기존 M12 cost-divergence escalation과 **OR 결합** — 둘 중 하나라도 발화하면 escalate

**DoD**:
- 합성 tampering 테스트: SW band를 의도적으로 변조 → linkage_consistency가 nonzero로 변함 + escalation 발화
- T2 모드 (HW band가 0인 경우) → linkage_consistency 도 0 (의미 있는 신호가 없으니까)

**예상 작업량**: 4일

---

#### M26 — ZK range proof for cost dimensions (stretch)

**특허 reference**: Claim 40 (선택적 stretch goal)

M12 의 `selective_disclosure` 가 대상 cost record 의 **존재**만 증명한다. M26은 cost 값의 **범위**를 증명하면서 정확값은 숨김.

- Bulletproofs (range proof) 또는 zk-STARK (Cairo, Polygon zkEVM-style) 둘 중 하나
- 예시 use case: "이 agent의 cumulative_dollars는 $100..$200 범위 안에 있다 (정확값 미공개)" 를 ZK 로 증명
- Library 옵션:
  - `bulletproofs-pp` (Rust → Python via PyO3)
  - `arkworks-rs` (Rust)
  - 둘 다 Python 패키징은 직접 빌드 필요
- T3의 cost_attestation 응답에 새 필드 `cost_range_proof: hex` 옵션으로 추가

**DoD**:
- `GET /cost-attestation/{aid}?disclosure=range` → ZK proof 반환
- External verifier 로 검증 가능 (별도 CLI tool 제공)

**예상 작업량**: 14일 — 우선순위 가장 낮음. M17–M25 모두 완료 후 stretch.

---

## 3. Phase / 의존성 그래프

```
                ┌────────────────────────────────────────────────┐
                │  Phase A — TEE software path                   │
                │  (cloud-only, ~12일)                           │
                ├────────────────────────────────────────────────┤
                │                                                │
                │   M17 TEE attestation                          │
                │      │                                         │
                │      ▼                                         │
                │   M18 ML-DSA dual-sign  ◀────  M17 X 무관      │
                │      │                                         │
                │      ▼                                         │
                │   M19 HW cost counters (RAPL/NVML)             │
                │                                                │
                └────────────┬───────────────────────────────────┘
                             │
                ┌────────────▼───────────────────────────────────┐
                │  Phase B — In-storage / accelerator (~42일)    │
                ├────────────────────────────────────────────────┤
                │                                                │
                │   M20 FPGA sLLM judge                          │
                │      ▲                                         │
                │      │ (M19 의 cost counters로 power 측정)     │
                │                                                │
                │   M21 HW tag comparator (VT-d/IOMMU)           │
                │      ▲                                         │
                │      │ (M14의 step315 정책 reuse)              │
                │                                                │
                │   M22 CSD integration                          │
                │      ▲                                         │
                │      │ (M16의 HAM API contract reuse)          │
                │                                                │
                └────────────┬───────────────────────────────────┘
                             │
                ┌────────────▼───────────────────────────────────┐
                │  Phase C — Cross-cutting hardening (~14일+)    │
                ├────────────────────────────────────────────────┤
                │                                                │
                │   M23 Per-AID HW counters  ← M19 후행         │
                │   M24 TEE key sealing      ← M17 후행         │
                │   M25 Linkage consistency  ← M19+M23 후행     │
                │   M26 ZK range proof       ← stretch          │
                │                                                │
                └────────────────────────────────────────────────┘
```

**필수 ordering**:
- **M17 → M18, M24**: TEE 환경이 있어야 sealing이 의미 있음. M18 ML-DSA는 TEE 없이도 가능하지만 M17과 함께 묶는 게 자연스러움.
- **M19 → M23, M25**: per-AID HW counter (M23)와 linkage consistency (M25) 모두 시스템 HW counter (M19) 가 우선되어야 함.
- **M14 → M21**: HW tag comparator는 M14의 정책 파일·API contract를 그대로 reuse. M14 → M21 substitution.
- **M16 → M22**: CSD는 HAM의 L2 backing. M16의 HAM endpoints는 변하지 않음.
- **M13 → M20**: FPGA sLLM은 Haiku attribution head를 substitution. M13 정의한 30-bar attribution 차트가 그대로 동작.

**병렬 가능**:
- M18, M19 — 둘 다 M17 직후 동시 진행 가능
- M21, M22 — Phase B 안에서 서로 독립

---

## 4. 하드웨어 procurement matrix

각 milestone에 필요한 hardware. 클라우드만으로 가능한 것 vs 실제 하드웨어 구매가 필요한 것을 분리.

| Milestone | 필수 HW | 클라우드 옵션 | 자체 구매 옵션 | 예상 비용 |
|---|---|---|---|---|
| M17 | Intel TDX or AMD SEV-SNP | **Azure DCsv5** ($1.50/h), **GCP C3 Confidential** ($1.20/h), **AWS R7iz** ($2.50/h) | Intel Sapphire Rapids ($800+) or AMD EPYC Genoa | $0 (단기 검증), $800+ (지속 호스팅) |
| M18 | — (CPU만) | 어디서든 | 어디서든 | $0 |
| M19 | Linux RAPL + NVIDIA GPU (옵션) | 모든 GPU 인스턴스 (DCGM 지원) | 기존 Mac mini는 RAPL 없음 → Linux 서버 필요 ($500+) | $500+ |
| M20 | **FPGA/AIE board** | (없음 — Vitis AI는 로컬 빌드) | **Xilinx Versal AI Edge VEK280** ($2,000), **AMD MI300X cloud** ($5/h on Lambda) | $2,000 (board) or $1,000+/mo (cloud) |
| M21 | Bare-metal Linux + Intel VT-d / AMD IOMMU | **불가** (대부분 cloud 인스턴스 IOMMU 미노출) | Intel Sapphire Rapids workstation ($1,500) | $1,500 |
| M22 | NVMe-CSD | **불가** | **Solidigm CSD-2000 eval program** (무상 + NDA), **Samsung SmartSSD** (~$3,000) | $0 (NDA) or $3,000 |
| M23 | M19와 동일 | M19와 동일 | M19와 동일 | $0 (M19에 포함) |
| M24 | M17와 동일 | M17와 동일 | M17와 동일 | $0 (M17에 포함) |
| M25 | — | — | — | $0 |
| M26 | — | — | — | $0 |

**전체 예산 (현실적)**:
- **Lean path**: M17, M18, M19, M25 만 → 클라우드 $50/월 + Linux 서버 $500 일회성 = **$50–600**
- **Full Phase A+B (M17–M22)**: + Xilinx FPGA board + bare-metal IOMMU + Solidigm NDA = **$3,500–6,500**
- **Stretch (M26 포함)**: + ZK proof 라이브러리 빌드 시간 = 같음

---

## 5. 리스크 레지스터

| 리스크 | 가능성 | 영향 | 완화책 |
|---|---|---|---|
| Vitis AI 5.0의 1B-param LLM 지원 미성숙 (M20) | 중 | 큼 | Plan B: Hailo-15 또는 Apple Neural Engine via CoreML. ML quant 4-bit 대신 8-bit으로 후퇴. |
| CSD eval program NDA 거절 (M22) | 낮음 | 중 | Plan B: Linux loop device + io_uring 기반 emulation으로 API contract만 확정. 실제 hardware는 후속. |
| TDX/SEV-SNP TCB validator API 변경 (M17) | 중 | 작 | Intel/AMD 공식 SDK pin + dependabot ignore. 분기 release로 검증. |
| ML-DSA 표준 변경 (NIST FIPS 204 → 최종본) (M18) | 중 | 작 | `oqs-python` 의 algorithm OID로 분기. 표준 확정 후 단일 commit으로 마이그레이션. |
| IOMMU programming 권한 (M21) | 높음 | 큼 | Bare-metal 전제 명시. Container 안에서 동작 못 함을 README에 박음. |
| HW counter overhead가 latency budget 초과 (M19) | 낮음 | 중 | RAPL은 ~1ms, NVML은 ~0.5ms. 누적해서 < 5ms 예상. 초과 시 background 스레드로 폴링 + cache. |
| 기존 T2 deployment에 강제 업그레이드 압박 (모든 M) | 낮음 | 작 | T2 코드는 그대로 유지. T3는 substitution이지 replacement 아님. `tier_profile` 한 글자만 다름. |

---

## 6. 테스트 전략

### CI에서 가능한 것

- **M17**: TDX/SEV-SNP simulator (Intel `tdx-attest-rs` 의 mock mode, AMD `sev-snp-utils` simulator). PR마다 시뮬레이터 통과 검증
- **M18**: `oqs-python` software-only 모드. CI에서 모든 ML-DSA 단위 테스트 실행 가능
- **M19**: Linux container에서 RAPL은 cap_sys_rawio 권한 필요 → CI는 mocked counter. Hardware-in-loop는 nightly로 실제 GPU runner.
- **M20**: FPGA bitstream 시뮬레이터 (Vitis HLS C-sim) → CI에서 결과 비교. Bit-exact는 hardware-in-loop nightly만.
- **M21**: VT-d simulator 없음 → CI는 software fallback path만 검증. Hardware-in-loop weekly.
- **M22**: Linux loop device emulation → CI에서 API contract 검증. CSD-specific code path는 hardware-in-loop only.

### Hardware-in-loop runner

추가 필요:
- 1× Linux Sapphire Rapids workstation (TDX + IOMMU + RAPL): M17, M19, M21, M23, M24
- 1× Xilinx VEK280 부착된 Linux 호스트: M20
- 1× NVMe-CSD 부착된 Linux 호스트: M22

이 3개를 하나의 박스에 모으는 것도 가능 (Sapphire Rapids workstation에 PCIe 슬롯으로 VEK280 + CSD 추가).

### 회귀 보호

- 모든 T3 milestone은 **T2 fallback path가 항상 통과**해야 함. CI는 T2 모드만으로 326 tests 그대로 통과 확인 → T3 코드가 T2 사용자를 깨지 않도록 보장.
- New env var `AEGIS_TIER_PROFILE=T2|T3|auto` (default `auto`). `auto`는 hardware probing 결과로 결정.

---

## 7. 특허 Claim 매핑 (T3 보강)

PLAN_v2 §7의 매트릭스에 T3 milestone 칼럼 추가:

| Claim # | 요약 | T2 커버리지 | T3 milestone |
|---|---|---|---|
| 1 | TEE-signed commitment + tamper-evident audit | SW signing only | **M17 (TEE quote), M24 (TEE-sealed key)** |
| 3 | Dual-band cost (SW+HW) + divergence + TEE-signed cost ledger | SW only | **M17, M19, M23** |
| 6 | HW band subfields | placeholder zero | **M19, M21, M23, M25** |
| 8 | 0.1-1B sLLM 3-class classifier | Claude Haiku | **M20 (FPGA bitstream)** |
| 10 | FPGA/AIE 배포 | — | **M20** |
| 11 | bit-exact deterministic sLLM | temperature=0 | **M20 (Vitis AI deterministic kernel)** |
| 12 | linkage_consistency_features | placeholder zero | **M25** |
| 17 | SHA3-256 + Ed25519/ML-DSA | Ed25519 only | **M18** |
| 21 | CSD device | — | **M22** |
| 22 | CSD + AID enforcement at memory controller | step315 software | **M21 (VT-d/IOMMU), M22 (CSD)** |
| 25 | ML-DSA post-quantum dual-signing | stub | **M18** |
| 27 | Shadow-phase baseline + cost divergence + 독립 escalation | SW divergence only | **M19 (HW divergence), M25 (linkage)** |
| 31 | CSD method claim | — | **M22** |
| 40 | ZK range proof | — | **M26 (stretch)** |

**T3 완료 시 patent coverage**: 40개 claim 중 **38개 fully covered** (Claim 35-39는 means-plus-function 매핑으로 자동, Claim 40은 stretch).

---

## 8. 외부 contract — 변경 vs 무변경

### 변경 (additive만)

- 새 env var: `AEGIS_TIER_PROFILE`, `AEGIS_TEE_PROVIDER` (`tdx`|`sev-snp`|`none`), `AEGIS_FPGA_BITSTREAM_PATH`, `AEGIS_CSD_DEVICE`
- 새 endpoint: `GET /attestation/tee-quote`, `GET /hw-counters/{aid}`
- 새 응답 필드: 모든 signed record에 `signature_ml_dsa` 추가
- ATVHeader 새 값: `tier_profile="T3"`, `cost_attestation_profile="hardware"|"both"`, `model_hash` (FPGA bitstream hash)

### **무변경 (가장 중요)**

- 모든 기존 endpoint URL + 메서드 + 요청 body shape
- ATV-2080-v1 차원·subfield 인덱스·`schema_version`
- Verdict JSON shape (`decision`, `reason`, `atv_id`, `signature`, `step_traces`)
- Audit chain Merkle 알고리즘 (SHA3-256)
- Ed25519 서명 검증 절차 (T3에서 ML-DSA 추가는 additive)
- 4개 키 (telemetry/cost/journal/HAM) 의 logical 분리 (Claim 34)

→ **T2 client는 코드 변경 없이 T3 server와 통신 가능**. 단지 응답에 새 필드가 보일 뿐.

---

## 9. 마이그레이션 전략

### T2 → T3 전환 (single deployment)

1. T2 모드로 정상 동작 확인 (`tier_profile="T2"`, 326 tests pass)
2. `AEGIS_TIER_PROFILE=auto` 로 boot
3. M17의 TEE probing 실행:
   - TDX 가용? → `cost_attestation_profile="both"`, `tier_profile="T3"` 로 자동 승격
   - 미가용? → T2 모드 유지 (no-op)
4. M18의 ML-DSA 키 자동 생성 (TEE 가용 시 sealed, 미가용 시 평문 — 단 deprecation warning)
5. M19의 HW counter probing → 가용 counter만 채움, 미가용은 zero
6. 기존 audit 체인은 그대로. 새 record부터 dual-signed + HW band 채워짐.
7. 외부 verifier는 `signature_ed25519` 만 보면 backward-compat. ML-DSA를 추가로 검증하면 forward-compat.

### Audit chain 호환성

기존 T2 record는 single-signed. T3 첫 record는 dual-signed. Chain validity는 prev_hash 만 계속 이어지면 OK — 서명 알고리즘이 변해도 chain은 깨지지 않음. Verifier는 record별로 어떤 알고리즘을 검증할지 결정하면 됨.

---

## 10. Definition of Done (T3 종합)

T2 DoD (PLAN_v2 §8) 는 모두 그대로 유지. T3 추가 DoD:

- `docker compose up` (TDX VM) → `/attestation/tee-quote` 가 valid TDX quote 반환 + Intel `dcap-quote-verification` 으로 검증 통과
- `/audit/{aid}` 응답에 `signature_ml_dsa` 필드 존재 + `liboqs-python` verifier로 검증 통과
- `/cost-attestation/{aid}` 가 `sw_cost_metrics` + `hw_cost_metrics` 둘 다 포함
- FPGA가 부착된 환경에서 sLLM judge call의 `model_hash` 가 bitstream SHA3-256과 일치
- `linkage_consistency_features` 가 nonzero (의도적 SW band 변조 시 magnitude 증가)
- 326개 T2 테스트 + 신규 T3 테스트 (~120개 예상) 통과
- T2 모드로 부팅 시 326 tests 그대로 통과 (회귀 0)
- mypy strict 유지 (소스 파일 수 ~80개로 증가 예상)
- 모든 Claim 1, 3, 6, 8, 10, 11, 12, 17, 21, 22, 25, 27, 31의 ASCII-검증 가능 요소가 자동 검증됨

---

## 11. 타임라인 (현실적 예측)

`PLAN_v2`는 하루 3-5시간 가정으로 22일이었다. T3는 하드웨어 dependency 때문에 같은 가정으로는 더 오래 걸린다. **풀타임 1명 기준**:

| Phase | Milestones | 예상 작업량 | 누적 |
|---|---|---|---|
| **A. TEE software** | M17, M18, M19 | 4 + 3 + 5 = **12일** | 12일 |
| **B1. FPGA judge** | M20 | **14일** | 26일 |
| **B2. HW tag comparator** | M21 | **7일** | 33일 |
| **B3. CSD integration** | M22 | **21일** | 54일 |
| **C. Cross-cutting** | M23, M24, M25 | 5 + 5 + 4 = **14일** | 68일 |
| **Stretch** | M26 (옵션) | **14일** | 82일 |

**핵심 milestone (M17–M19)** 만 우선 추진하면 **2-3주**로 patent의 핵심 HW claim (TEE attestation, ML-DSA, HW cost counter) 을 모두 커버. Phase B는 hardware procurement에 따라 **6-12개월** 단위로 분할 진행하는 것이 현실적.

---

## 12. 진행 상태 (placeholder)

> 이 섹션은 milestone 완료 시마다 업데이트된다. PLAN_v2 §"진행 상태" 와 동일 포맷.

| Milestone | 상태 | 주요 성과 | 신규 테스트 |
|---|---|---|---|
| **M17 TEE attestation** | **✅ 완료 (mock provider)** | `aegis.attest.tee_quote` 모듈 + `TEEQuote` envelope (TDX/SEV-SNP/mock unified shape). `GET /attestation/tee-quote` endpoint. `AEGIS_TEE_PROVIDER=mock` 으로 CI에서도 동작. 실제 TDX/SEV-SNP 코드 경로는 placeholder (device 미존재 시 mock으로 graceful degrade). burn_in_id 가 quote의 `report_data` 에 박힘 — T2 measurement 가 T3 quote에 cryptographically bound. | +21 (455 total) |
| M18 ML-DSA dual-sign | 🔜 | — | — |
| M19 HW cost counters | 🔜 | — | — |
| M20 FPGA sLLM | 🔜 (HW 의존) | — | — |
| M21 HW tag comparator | 🔜 (HW 의존) | — | — |
| M22 CSD integration | 🔜 (HW 의존) | — | — |
| M23 Per-AID HW counters | 🔜 (M19 후행) | — | — |
| M24 TEE-sealed keys | 🔜 (M17 후행) | — | — |
| M25 Linkage consistency | 🔜 (M19+M23 후행) | — | — |
| M26 ZK range proof | 🔜 (stretch) | — | — |

---

## 13. 변경 관리

- 특허 문구가 개정되면 이 문서 상단에 **변경 요약** 섹션 추가 + 영향받는 Milestone 표시 (PLAN_v2와 동일 정책)
- `policies/claim_map.json` 의 T3 milestone column 자동 업데이트 (`tools/claim_coverage.py` — PLAN_v2 §10에서 예고된 스크립트)
- HW vendor SDK 버전 변경은 `pyproject.toml` extras 그룹으로 격리 (`uv sync --extra t3-tdx`, `--extra t3-fpga`, etc.)

---

## 14. 다음 1주에 할 일

PLAN_v3 승인되면 즉시 시작 가능한 작업:

1. **Day 1**: Azure DCsv5 또는 GCP C3 Confidential 인스턴스 1개 준비. `docker compose up -d` 로 T2 그대로 부팅 확인.
2. **Day 2-3**: `aegis.attest.tee_quote` 모듈 신규 작성 (Intel DCAP `tdx-attest-rs` Python wrapper). `/attestation/tee-quote` endpoint.
3. **Day 4**: M17 단위 테스트 + Intel TCB validator로 quote 검증.
4. **Day 5-6**: `oqs-python` 통합 + ML-DSA 키 자동 생성. 모든 record에 dual-sign 추가.
5. **Day 7**: M17 + M18 e2e 테스트. 326 tests 회귀 0 확인. 첫 T3 commit.

이 시점에서 코드는 **T2 mode로는 완전 동일하게 동작 + T3 mode (cloud TEE 위) 로는 hw-rooted attestation + post-quantum dual-sign 추가**. 핵심 Claim 1, 17, 25를 hardware-rooted로 커버.

이후 M19부터는 dedicated Linux server (RAPL 가용) 가 필요하므로, hardware procurement 과 병행.

---

**문서 끝**. 이 v3 계획은 PLAN_v2의 T2 software 자산을 그대로 유지한 채 patent v7.10의 hardware claim들을 substitution 방식으로 채우는 증분 명세서다. T2 → T3는 rewrite가 아니라 **substitution** 이다 — 외부 contract는 한 글자도 변하지 않으며, T2 client는 T3 server와 코드 변경 없이 통신할 수 있다.

대상 독자: AegisData T3 deployment를 직접 만들 사람, 또는 T2 코드의 substitution boundary가 어디인지 알고 싶은 contributor.
