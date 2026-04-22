# T2 → T3 Substitution Boundary

The patent's T2 (software) and T3 (hardware) tiers share **a single
external contract** — same schema, same endpoints, same JSON shapes.
T3 is a substitution exercise, not a rewrite.

This doc is the line-by-line "what changes, what doesn't" reference
for someone implementing T3 on top of the existing T2 codebase. For
the milestone-level plan with timelines, claim mapping, and risk
register, see [`PLAN_v3.md`](../PLAN_v3.md).

---

## TL;DR

- **External contract: zero changes.** A T2 client talks to a T3
  server with no code change. The only visible difference is that
  some response fields stop being zero.
- **Internal code: ~10 new modules, ~5 modules with substitution
  hooks.** Existing T2 implementations are kept as fallbacks; T3
  modules subclass or wrap them.
- **Auto-detection: a single env var (`AEGIS_TIER_PROFILE=auto`)
  probes for hardware on boot and decides T2 or T3 mode at runtime.**

---

## What stays identical (the contract)

These don't move when crossing T2 → T3. **Don't touch.**

| Surface | Why it stays |
|---|---|
| ATV-2080-v1 dimension count, subfield indices, `schema_version` | Patent Claim 24 — single schema for T2/T3 uniformity |
| Every endpoint URL + HTTP method + request body shape | T2 clients keep working unchanged |
| `Verdict` JSON shape (decision/reason/atv_id/signature/step_traces) | Same |
| Audit chain Merkle algorithm (SHA3-256) + chain head format | Same |
| Ed25519 signature verification procedure | T3 *adds* ML-DSA cosign but keeps Ed25519 |
| The 4-key separation (telemetry / cost / journal / HAM) | Patent Claim 34 |
| All policy file formats (`policies/*.json`) | Same — even M21's HW tag comparator reads the same `aid_region.json` |
| ATMU state machine (7 states, legal transitions) | Same — T3's hardware checkpointing just changes the storage backing |

---

## What changes (additive only)

### New env vars

| Variable | Default | Effect |
|---|---|---|
| `AEGIS_TIER_PROFILE` | `auto` | `T2` forces software, `T3` forces hardware (errors if HW absent), `auto` probes |
| `AEGIS_TEE_PROVIDER` | `none` | `tdx`, `sev-snp`, `none` — selects the M17 attestation backend |
| `AEGIS_FPGA_BITSTREAM_PATH` | unset | Path to the M20 FPGA sLLM bitstream; if set, judge is on-FPGA |
| `AEGIS_CSD_DEVICE` | unset | Path to the M22 NVMe-CSD device node (e.g. `/dev/nvme1`) |
| `AEGIS_HW_PROFILE_AID` | `false` | Enable per-AID HW counter capture (M23) — has cgroup overhead |

### New endpoints (additive)

| Method | Path | Returns | Milestone |
|---|---|---|---|
| GET | `/attestation/tee-quote` | TDX/SEV-SNP quote + collateral | M17 |
| GET | `/hw-counters/{aid}` | Per-AID HW resource snapshot | M23 |
| GET | `/hw-counters/_all` | Aggregated system-wide HW snapshot | M23 |

### New response fields

These appear in **existing** endpoints but are additive — clients
that don't read them are unaffected.

| Endpoint | New field | Type | Milestone |
|---|---|---|---|
| `GET /audit/{aid}` records | `signature_ml_dsa` | hex string (~6.5 KB) | M18 |
| `GET /attestation` | `tee_quote_ref` | URL pointer to `/attestation/tee-quote` | M17 |
| `GET /attestation` layers | `L1_hardware_ek.tee_measurement` | hex (TDX MRTD or SEV-SNP launch measurement) | M17 |
| `POST /evaluate` Verdict | `step_traces["judge.fpga_aie"]` | string with bitstream hash | M20 |
| `GET /cost-attestation/{aid}` records | `hw_cost_metrics` | object (mirrors `sw_cost_metrics`) | M19 |
| `GET /cost-attestation/{aid}` records | `sw_vs_hw_divergence` | 3-metric object | M19 |
| (any signed record) | `key_versions: {ed25519, ml_dsa}` | object | M18 |

### ATVHeader new values (already permitted by the schema)

| Field | T2 typical | T3 typical |
|---|---|---|
| `tier_profile` | `"T2"` | `"T3"` |
| `cost_attestation_profile` | `"software"` | `"both"` (software always also runs as a sanity check) |
| `node_id` | unset | TDX MRTD or SEV-SNP launch measurement (hex) |
| `model_hash` | unset | FPGA bitstream SHA3-256 (M20) |
| `burn_in_id` | source-hash-based | TEE-quote-based (M17) |

### HW band — the 11 zero-filled subfields

This is the meat of T3. Each subfield gets a real encoder.

| Slice (indices) | T2 | T3 implementation | Module path |
|---|---|---|---|
| `memory_timing_histograms` 1880..1911 | zeros | DRAM perf counters via `/sys/devices/uncore_imc/`, EDAC stats | `aegis.atv.encoders.hw_memory_timing` |
| `aid_tag_transitions` 1912..1935 | zeros | HW tag comparator transition counts + violation flags | `aegis.atv.encoders.hw_aid_tag` |
| `atmu_anomaly` 1936..1951 | zeros | TEE-bound 2PC violation counter (intent_log monitor export) | `aegis.atv.encoders.hw_atmu_anomaly` |
| `dma_fanout` 1952..1967 | zeros | IOMMU stats (Intel VT-d, AMD IOMMU), CXL.mem traffic | `aegis.atv.encoders.hw_dma` |
| `thermal_ecc_drift` 1968..1983 | zeros | On-die thermal sensors + EDAC ECC counters | `aegis.atv.encoders.hw_thermal_ecc` |
| `watchdog_signals` 1984..1995 | zeros | TPM watchdog, BMC heartbeat, TEE quote freshness | `aegis.atv.encoders.hw_watchdog` |
| `network_telemetry` 1996..2019 | zeros | NIC perf counters, eBPF-tracked SmartNIC stats | `aegis.atv.encoders.hw_network` |
| `gpu_accelerator_state` 2020..2035 | zeros | NVIDIA NVML/DCGM, AMD ROCm, Intel HBI | `aegis.atv.encoders.hw_gpu` |
| `hypervisor_signals` 2036..2043 | zeros | TDX QGS / SEV-SNP attestation report scalars | `aegis.atv.encoders.hw_hypervisor` |
| `hw_cost_attestation` 2044..2059 | zeros | RAPL / MSR perf counters / DCGM power | `aegis.atv.encoders.hw_cost_attestation` |
| `linkage_consistency_features` 2060..2079 | zeros | Computed from cross-band tampering checks (M25) | `aegis.atv.encoders.linkage_consistency` |

Each new encoder is a drop-in replacement for the existing
`zero_fill` encoder for that slice. The `build_atv()` function in
`aegis.atv.builder` already iterates the encoder table — adding
new entries is a single dict update.

---

## Substitution map (T2 module → T3 module)

For each T2 module that gets a T3 substitute, the table shows
what's replaced and what stays.

### `aegis.attest.code_attestation` → `aegis.attest.tee_quote` (M17)

| Aspect | T2 | T3 |
|---|---|---|
| Measurement root | `sha3_256(source files + policies + signing key)` | TDX MRTD or SEV-SNP launch measurement |
| Signature | Ed25519 over the measurement | TEE quote with `report_data = T2 measurement` |
| Endpoint | `GET /attestation` (Ed25519-signed) | `GET /attestation` (same shape, root replaced) + `GET /attestation/tee-quote` (raw quote) |
| Verifier | Any Ed25519 library | Intel `dcap-quote-verification` or AMD `snpguest` |

The T2 measurement is **embedded inside** the T3 quote's
`report_data` field. So a T3 attestation is provably bound to T2's
source-hash measurement — defense in depth.

### `aegis.judge.haiku` → `aegis.judge.fpga_aie` (M20)

| Aspect | T2 | T3 |
|---|---|---|
| Backend | Anthropic Haiku 4.5 (network) | Pinned 0.1–1B model on Xilinx Versal AI Edge / AMD MI300X |
| Inputs | ATV-2080-v1 + system prompt | ATV-2080-v1 (no prompt — fine-tuned on AegisData policy corpus) |
| Outputs | verdict + confidence + attribution (JSON) | verdict + confidence + 30-D attribution vector (raw tensor) |
| Determinism | temperature=0 (best-effort) | bit-exact across runs (Vitis AI deterministic kernel) |
| Latency | ~180 ms | < 50 ms target |
| Verifiability | model name in trace | `model_hash` (bitstream SHA3-256) in `ATVHeader` |

T2's `JudgeVerdict` shape is unchanged — T3 just fills it from a
different source. The `step340_policy.run()` function selects
backend by `AEGIS_FPGA_BITSTREAM_PATH` env var.

### `aegis.firewall.step315_aid_auth` → `aegis.firewall.hw_tag_comparator` (M21)

| Aspect | T2 | T3 |
|---|---|---|
| Enforcement point | Python middleware before step 320 | Memory controller (Intel VT-d / AMD IOMMU) |
| Policy source | `policies/aid_region.json` | Same JSON, compiled to IOMMU page tables at boot |
| Violation handling | `_breaker.record_violation()` Python call | IOMMU page fault → kernel module → `_breaker.record_violation()` |
| Quarantine | `is_quarantined()` Python check | IOMMU page table flush — agent's pages become unmapped |

The `aid_region.json` policy file is **the same file**. The breaker
state is **the same in-memory object**. T3 just promotes the
enforcement from Python to silicon.

### `aegis.ham.store` → `aegis.ham.csd_l2` (M22)

| Aspect | T2 | T3 |
|---|---|---|
| L1 cache | OrderedDict in-process | Same OrderedDict (HBM-backed register cache in M27 stretch) |
| L2 NVMe-tier | (not implemented) | NVMe-CSD with in-storage similarity |
| L3 object store | SQLite (encrypted bodies, AES-GCM with AAD) | Same SQLite — moves to slow tier |
| L4 cold archive | (same SQLite, conceptually L4) | Same |
| Recall path | host CPU pulls + decrypts | CSD does similarity + returns top-K with bodies still encrypted |

T2's M16 endpoints (`/ham/memory`, `/ham/recall`, etc.) don't
change. T3 just adds a faster L2 tier underneath.

### `aegis.sign.ed25519` → `aegis.sign.dual_sign` (M18)

| Aspect | T2 | T3 |
|---|---|---|
| Sign | Ed25519 only | Ed25519 + ML-DSA-65 |
| Verify | Ed25519 only | Either or both — defense-in-depth (any failure rejects) |
| Key storage | `keys/ed25519.pem` + `_cost.pem` | Same + `keys/ml_dsa_65.pem` + `_cost.pem`. M24 wraps all in TEE sealing. |
| Record size | ~3.5 KB | ~7 KB (ML-DSA-65 sig is ~3.3 KB) |

### `aegis.cost.divergence` → `aegis.cost.divergence` (extended, M19)

Existing module gets new functions. Old functions unchanged.

```python
# T2 (already exists):
compute_divergence(sw_metrics, hw_metrics)  # 3-metric SW-only

# T3 (new):
compute_sw_vs_hw_divergence(sw_metrics, hw_real)  # additional pair
```

### `aegis.attest.code_attestation` keys → `aegis.sign.tee_sealed` (M24)

After M24, the four keys (telemetry/cost/journal/HAM) live as
sealed blobs:

| Path | T2 | T3 |
|---|---|---|
| `keys/ed25519.pem` | private key (PEM) | replaced by `keys/ed25519.sealed` |
| `keys/ed25519.pub` | public key (PEM) | unchanged |
| `keys/ed25519_cost.pem` | private key | replaced by `keys/ed25519_cost.sealed` |
| `keys/journal_data.key` | 32 raw bytes | replaced by `keys/journal_data.sealed` |
| `keys/ham_data.key` | 32 raw bytes | replaced by `keys/ham_data.sealed` |

Migration on first T3 boot: TEE unseals → reads plaintext from
disk → re-seals → secure-wipes the plaintext file. One-way
migration; rollback to T2 requires regenerating keys.

---

## Things you might assume need to change but don't

### Storage layer

`data/audit.sqlite`, `data/journal.bin`, `data/ham.sqlite`,
`data/intent_log.sqlite`, `data/cost_attestation.sqlite` — all stay
exactly the same files with the same schemas. T3 just appends new
records (with longer signatures from M18, with HW band populated
from M19+). Old T2 records remain valid.

### Public keys

The Ed25519 public keys in `keys/*.pub` keep verifying both T2 and
T3 records. The new ML-DSA public key is published alongside as
`keys/ml_dsa_65.pub` from M18 onward. External verifiers can choose
to verify either signature.

### Dashboard

The `/` dashboard reads existing endpoints. After T3:

* New `tier_profile: "T3"` chip appears in the header
* HW band strip (currently gray) shows real color values per subfield
* "Burn-in attestation" panel shows TEE quote + the original
  source-hash measurement nested as `report_data`
* "Forensic replay" panel gains a `signature_ml_dsa_count` tile
* "Hierarchical Agent Memory" panel gains a new "L2 (CSD)" tier row

All other panels stay byte-identical.

### Tests

T2's 326 tests must continue to pass on T3 hardware in T2 mode
(`AEGIS_TIER_PROFILE=T2`). T3 tests are additive (estimated ~120
new tests across M17–M26). The HW-in-loop subset runs on dedicated
runners, not in CI.

---

## How to introduce a new T3 milestone (process)

1. **Read the patent claim** the milestone implements. PLAN_v3 §7
   has the mapping.
2. **Identify the T2 placeholder** it substitutes. This doc's
   tables are the index.
3. **Write the T3 module** as a strict substitute — same input, same
   output shape. The T2 implementation stays as the fallback.
4. **Add a feature flag** (env var or config) that selects the
   backend. Default: `auto` (probe for hardware).
5. **Add HW-in-loop tests** in a separate test directory
   (`tests/hardware_in_loop/`) so they don't run in CI by default.
6. **Update the schema-version table** (PLAN_v3 §7) to mark the
   claim as covered.
7. **Update this document** with the new substitution row.

---

## Glossary

* **TEE** — Trusted Execution Environment (Intel TDX, AMD SEV-SNP,
  ARM CCA, NVIDIA H100 confidential)
* **TDX** — Intel Trust Domain Extensions (Intel's TEE for VMs)
* **SEV-SNP** — AMD Secure Encrypted Virtualization, Secure Nested
  Paging
* **MRTD / MRENCLAVE** — Measurement of Trust Domain (TDX) /
  enclave (SGX) — a hash of the launched code
* **CSD** — Computational Storage Device (NVMe SSD with on-device
  compute)
* **AIE** — AI Engine (Xilinx Versal accelerator tile)
* **VT-d / IOMMU** — Intel Virtualization Technology for Directed
  I/O / I/O Memory Management Unit (Intel/AMD)
* **PASID** — Process Address Space ID (used by VT-d for per-process
  memory isolation)
* **CXL** — Compute Express Link (interconnect for memory pooling
  and disaggregation)
* **ML-DSA** — Module-Lattice-based Digital Signature Algorithm
  (NIST FIPS 204, formerly CRYSTALS-Dilithium)
* **Bulletproofs** — Short non-interactive zero-knowledge proofs
  for range claims (used in M26 stretch)
* **RAPL** — Running Average Power Limit (Intel CPU power telemetry)
* **NVML / DCGM** — NVIDIA Management Library / Data Center GPU
  Manager (GPU telemetry)
