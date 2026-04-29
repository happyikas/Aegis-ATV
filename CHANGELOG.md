# Changelog

All notable changes to AegisData MVP. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [4.0.0] — 2026-04-29  ·  AuditPatrol — periodic background integrity check (Claim 54)

Closes the open question from the v3.9 whitepaper: "what catches
silent corruption / bit-rot / missing records *between* reads?"
v3.x verifies integrity **on demand** (Ed25519 sigs at write, AES-GCM
auth tags at decrypt, `aegis verify-audit` CLI). v4.0 adds a
**continuous background patrol** that walks the stores on its own
cadence and surfaces findings before the next reader trips over them.

### Added

* `src/aegis/audit/patrol.py` — `AuditPatrol` daemon with five patrol
  scopes:
  - **sequence** (5 min default) — ATMU (Agent Telemetry Management
    Unit) `intent_log.seq` gap detection
  - **sample** (1 h) — random 1 % subset; signature + SHA3 recompute
  - **consistency** (1 h) — cross-check SQLite ↔ JSONL ↔ encrypted
    journal (record presence + AEAD tag)
  - **full** (6 h) — every aid's chain in audit DB + cost ledger
    (Merkle + Ed25519)
  - **cold** (24 h) — sample N segments from the v3.9 cold tier and
    re-decrypt
  Each scope returns a `PatrolReport` with structured `PatrolFinding`s
  classified by category (signature, hash_mismatch, chain_break, aead,
  consistency, sequence_gap) and severity (warning, critical).
  Rolling 50-report history kept in memory for ops dashboards.
* `src/aegis/api/audit_patrol.py` — `GET /audit/patrol/status` and
  `POST /audit/patrol/run` endpoints.
* `src/aegis/main.py` — auto-wires the patrol when
  `AEGIS_AUDIT_PATROL_ENABLED=true`.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` adds **Claim 54** — periodic 6-check
  integrity attestation. T3 hardware (M19+) extension: patrol report
  itself signed under the cost-attestation key (Claim 34) so the
  patrol can't lie either.

### Config (all default off)

```bash
AEGIS_AUDIT_PATROL_ENABLED=true
AEGIS_AUDIT_PATROL_FULL_INTERVAL_SEC=21600          # 6h
AEGIS_AUDIT_PATROL_SAMPLE_INTERVAL_SEC=3600         # 1h
AEGIS_AUDIT_PATROL_SEQUENCE_INTERVAL_SEC=300        # 5min
AEGIS_AUDIT_PATROL_CONSISTENCY_INTERVAL_SEC=3600    # 1h
AEGIS_AUDIT_PATROL_COLD_INTERVAL_SEC=86400          # 24h
AEGIS_AUDIT_PATROL_SAMPLE_FRACTION=0.01             # 1 %
AEGIS_AUDIT_PATROL_COLD_SEGMENTS_PER_RUN=3
AEGIS_AUDIT_PATROL_POLL_SECONDS=30
```

### Tests

* `tests/unit/test_audit_patrol.py` (26 tests) — every scope covered
  with both clean-chain and corrupted-chain inputs (signature tamper,
  hash mismatch, sequence gap, AEAD tamper, JSONL drift, cold-tier
  decrypt). Lifecycle (start/stop/double-start). Endpoint integration.

### Numbers

* **1045 tests PASS** (1019 → 1045, +26), 1 skipped (llama-cpp).
* **mypy 102 source files clean.**
* **ruff clean.**
* All new functionality opt-in; existing test surface unaffected.

---

## [3.9.0] — 2026-04-28  ·  Production durability — group-commit + tiered archive

Bridges the gap between T2 demo (memory + per-call sync) and the four
production durability patterns documented in
`docs/WHITEPAPER_PERFORMANCE_KR.md` §2 (group commit / tiered / replicated
WAL / Raft). v3.8 ships pattern A; v3.9 ships pattern B.

### v3.8 — Group commit + persistent perf EWMA

* `src/aegis/audit/group_commit.py` — `GroupCommitEncryptedJournal`
  drop-in replacement for `EncryptedJournal`. Batches up to N records
  or `interval_ms` ms into a single `open() / write_all / fsync /
  close()` cycle. Each `append()` blocks until its batch is durable,
  preserving caller contract. On-disk format is bit-identical so the
  plain `EncryptedJournal` reads records group-committed earlier.
  `make_journal()` factory + flag-driven via
  `AEGIS_JOURNAL_GROUP_COMMIT`.
* `src/aegis/audit/encrypted_journal.py` — split `encrypt(record)` and
  `serialize(wrapper)` so wrappers can be staged without I/O. Plain
  `append()` now does `os.fsync(fileno())` for true durability
  (was previously `flush()` only).
* `src/aegis/performance/feedback_snapshot.py` —
  `PerfFeedbackSnapshotter` background daemon that periodically writes
  the v3.2 EWMA store to SQLite. Trigger:
  `min(interval_sec, updates_per_snapshot)` (default 30 s, 100).
  `load_into_store()` restores prior EWMA on boot so advisor confidence
  doesn't reset. Wired via `AEGIS_PERF_FEEDBACK_SNAPSHOT_DB`.

### v3.9 — Tiered archive (hot → cold)

* `src/aegis/audit/tiered_archive.py` —
  `TieredArchiveMigrator` background coordinator that:
  - Rotates the live journal file when it exceeds `rotate_bytes` or
    `rotate_seconds`.
  - Pushes closed segments to a pluggable `ArchiveBackend`:
    `FilesystemArchive` (default — `cp` to `cold_dir/`) or
    `S3ArchiveStub` (interface for S3/GCS/Azure Blob; production
    impl plugs in boto3).
  - Prunes hot tier after `hot_retention_segments` archived copies are
    safe.
* Encryption + commitment chain unchanged — replay still works against
  cold-tier files with the same data key.
* Wired via `AEGIS_TIERED_ARCHIVE_COLD_DIR`.

### Config changes

* `aegis_perf_feedback_snapshot_db` (path, default empty)
* `aegis_perf_feedback_snapshot_interval_sec` (default 30.0)
* `aegis_perf_feedback_snapshot_updates_threshold` (default 100)
* `aegis_journal_group_commit` (default False)
* `aegis_journal_group_commit_batch_size` (default 100)
* `aegis_journal_group_commit_interval_ms` (default 1.0)
* `aegis_tiered_archive_cold_dir` (path, default empty)
* `aegis_tiered_archive_rotate_bytes` (default 100 MB)
* `aegis_tiered_archive_rotate_seconds` (default 3600)
* `aegis_tiered_archive_hot_retention_segments` (default 3)
* `aegis_tiered_archive_poll_seconds` (default 10)

### Tests

* `tests/unit/test_feedback_snapshot.py` (11) — round-trip, trigger
  logic, lifecycle, simulated-restart EWMA continuity.
* `tests/unit/test_journal_group_commit.py` (10) — round-trip, durable-
  on-return, factory, validation, concurrent appends, cross-compat
  with plain journal, drain on close.
* `tests/unit/test_tiered_archive.py` (16) — backend, rotation,
  archive idempotency, hot-tier retention, lifecycle, encrypted-
  journal cross-tier replay.

### Numbers

* **1019 tests PASS** (982 → 1019, +37), 1 skipped (llama-cpp).
* **mypy 100 source files clean.**
* **ruff clean.**
* All new modules opt-in (off by default), so existing test surface
  is unaffected. T3 hardware (M19+) will swap the filesystem backend
  for a CSD-backed durable region.

---

## [3.7.0] — 2026-04-28  ·  Context window advisor

ATV-based **token-budget-aware** decision of which historical turns
to keep verbatim, summarise, or drop. Different axis from KV cache:
KV cache works at the runtime memory layer; context advisor works
at the prompt-construction layer. Both consume the same ATV.

### Added

* `src/aegis/performance/context_advisor.py` — pure function
  `(current_atv, history_atvs, history_turn_ids, history_token_costs,
  token_budget) → ContextAdvice` with `keep_verbatim_turn_ids`,
  `summarize_turn_ids`, `drop_turn_ids`, `expected_token_savings`,
  per-turn relevance scores, `advisor_hash`. Frozen weights (0.45
  state cosine, 0.20 progress match, 0.10 novelty proximity, 0.25
  recency with 8-turn half-life). Greedy ROI fit under token_budget.
* `src/aegis/api/advisory.py` — `POST /advisory/context` accepting
  current ATVInput + list of historical (turn_id, atv_input,
  token_cost) + token_budget.
* `demo/context_advisor.py` — 12-turn three-phase conversation,
  three budgets (5000 / 2000 / 800 tokens). Recent same-phase
  turns score 0.85+ → keep; older different-phase turns drop first.
* `tests/unit/test_context_advisor.py` — 14 unit tests covering
  pure-function shape, determinism, budget fit, recency tie-breaks,
  per-turn bucket consistency, latency, endpoint integration.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` — Claims 48–50 added:
  * **Claim 48** — context window advisory head over ATV history
    (implemented).
  * **Claim 49** — subfield-selective ATV diff compression (deferred).
  * **Claim 50** — unified head v2 with 5 outputs including context
    (deferred to v3.8).

### Numbers

* **982 tests PASS** (968 → 982, +14), 1 skipped (llama-cpp).
* **mypy 97 source files clean.**
* **ruff clean.**
* Latency: 0.087 ms for 50-turn history (M3 Mac).
* Demo savings: 50 % (budget=5000) / 67 % (2000) / 87 % (800)
  on a 12-turn 6050-token simulated history.

---

## [3.6.0] — 2026-04-28  ·  Performance advisory surface (v3.1 → v3.6)

The same ATV-2080 that powers the trust firewall now drives **out-of-band
performance advisory** for LLM serving runtimes. Six chained releases
land in one milestone.

### v3.1 — KV cache advisor

* `src/aegis/performance/kv_cache_advisor.py` — pure function
  `(atv, inp) → KVCacheAdvice` with `prefetch_segment_ids`,
  `evict_candidates`, `residency_class` (hot/warm/cold), `batch_key`,
  `speculative_decode`, `confidence`, `advisor_hash`.
* `src/aegis/api/advisory.py` — `POST /advisory/kv_cache`.
* Sub-millisecond, deterministic, advisory-only (runtime is the enforcer).

### v3.2 — Closed-loop perf feedback

* `src/aegis/performance/feedback.py` — thread-safe per-(tenant, aid)
  EWMA store (α=0.30). Process-wide singleton.
* `src/aegis/api/tool_outcome.py` — extended with optional
  `cache_hit_rate` / `context_utilization_ratio` / `tokens_per_second` /
  `runtime_latency_ms` / `memory_peak_bytes`. Updates the EWMA on
  receipt; returns the snapshot.
* `src/aegis/api/{advisory,evaluate}.py` — backfill `s-10/s-11` when
  the host hasn't measured. Host-supplied values are NEVER overwritten.

### v3.3 — Runtime adapters

* `integrations/mlx_lm/__init__.py` — `MLXLMAegisAdvisor`: residency →
  sliding_window (hot=16k, warm=4k, cold=2k); speculative → draft model.
* `integrations/llama_cpp/__init__.py` — `LlamaCppAegisAdvisor`:
  residency → kv_cache_dtype (f16/q8_0) + n_gpu_layers delta.
* `demo/runtime_closed_loop.py` — 8-turn simulated runtime, watches
  EWMA + advice confidence climb.

### v3.4 — Scheduling + Placement advisors

* `src/aegis/performance/scheduling_advisor.py` — `(priority_class,
  preempt_safe, max_concurrent_in_cohort, deadline_ms)`.
* `src/aegis/performance/placement_advisor.py` — `(layer_residency_plan,
  kv_quantisation_dtype, prefetch_window_tokens, swap_threshold_bytes)`.
  Demotes middle blocks under high pressure; T3 routes cold layers
  to CSD instead of CPU.
* New endpoints: `/advisory/scheduling`, `/advisory/placement`,
  `/advisory/all` (one-shot fan-out).

### v3.5 — vLLM integration shim + design doc

* `integrations/vllm/__init__.py` — `VLLMAegisAdvisor` posts to
  `/advisory/all` and projects onto `VLLMAdvice`.
* `docs/VLLM_INTEGRATION_DESIGN.md` — three plug points
  (`AegisAwareBlockManager`, `AegisAwareScheduler`, `AegisAwarePrefetcher`).

### v3.6 — M13 unified head

* `src/aegis/judge/unified_head.py` — `UnifiedHead.evaluate_unified()`
  composes the v2.5 AttributionHead with the v3.1 / v3.4 advisors
  in one ATV pass. `unified_hash` = SHA3-256 over the four advisor
  versions — audit replay catches any head change. Trust path is
  bit-identical to standalone AttributionHead.
* `POST /advisory/unified` — runtime gets trust + perf in one call.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` (Korean) — provisional supplement
  proposing Claims 41–47 extending the existing `ATV_v7_10` filing
  with the perf-advisory surface, closed-loop attestation, unified
  head, and advisor-as-hint protocol.

### Tests / lint / types

* **968 tests PASS** (905 → 968, +63), 1 skipped (llama-cpp).
* **mypy 96 source files clean.**
* **ruff clean.**

---

## [3.0.0] — 2026-04-28  ·  ATV-native sLLM stack: M13 + Phi + hybrid combiner

The patent's three-tier sLLM vision (Claims 8 / 9) lands as a working
hybrid stack. v2.5 + v2.6 + v3.0 ship together as v3.0.0.

### Added — v2.5 M13 AttributionHead

* **`src/aegis/judge/attribution_head.py`** — frozen 30-feature linear
  classifier that reads the 2080-D ATV vector directly (not a text
  summary). Hand-tuned weights in `models/m13_attribution_head_v1.json`,
  SHA3-256 hashed at load time as `model_hash`.
* `evaluate_full(summary, atv, inp)` returns a `JudgeVerdict` with the
  full 30-key `subfield_attribution` map populated for the first time
  (Dummy / Haiku had returned empty dicts).
* <1ms inference, IEEE-754 deterministic, auditable via the frozen
  weights' SHA3 hash.

### Added — v2.6 LocalPhiJudge

* **`src/aegis/judge/local_phi.py`** — Phi-4-mini-q4 / Llama-family
  local sLLM with three-mode dispatch:
  * **Real** — `AEGIS_JUDGE_MODEL_PATH=/path/to/phi.gguf` + `llama-cpp-
    python` installed: GGUF-loaded greedy-decode (T=0, top_k=1).
    `model_hash` = SHA3-256 of the GGUF file.
  * **Stub** — no path / `AEGIS_JUDGE_LOCAL_PHI_STUB=1`: delegates to
    M13 AttributionHead and re-labels the reason. Deterministic,
    audit-clean, CI-friendly.
  * **Disabled** — env points at missing file or llama-cpp-python
    missing: returns confidence=0.0 ALLOW so the v3.0 HybridJudge
    routes past it.
* Prompt embeds the M13 attribution top-5 contributors so the LM
  has structured signal alongside the summary.
* `_parse_real_decode` accepts both strict JSON output and keyword-
  fallback for robust small-model inference.

### Added — v3.0 HybridJudge

* **`src/aegis/judge/hybrid.py`** — confidence-routing combiner over a
  layered Judge stack. Default tiers, in increasing latency × cost ×
  non-determinism order:

  | Tier | Judge | Latency | Determinism |
  |---|---|---:|---|
  | 1 | `m13_attribution` (AttributionHead) | <1 ms | bit-identical |
  | 2 | `local_phi` (stub or real Phi-4-mini-q4) | <1ms / ~50 ms | bit-identical (stub) / attestable (real) |
  | 3 | `haiku` (Anthropic API, only when `ANTHROPIC_API_KEY` set) | ~150 ms | "approximately stable" |
  | 4 | `dummy` (regex) | <1 ms | bit-identical |

* Routing rule: a tier "commits" on BLOCK / REQUIRE_APPROVAL OR on
  ALLOW with `confidence ≥ allow_threshold`. Low-confidence ALLOW
  escalates to the next tier — the "fail-safe escalation" pattern.
* `JudgeVerdict.layer_traces` records each consulted tier's
  ``"name: decision conf=X.XX (T.Tms)"``. `model_hash` set to the
  *deciding* tier's hash so `aegis verify-audit` can re-run the
  exact path. `latency_ms` is the cumulative wall-clock.

### Changed

* **`src/aegis/judge/base.py`** — `JudgeVerdict` gains optional
  `model_hash`, `latency_ms`, `layer_traces` fields (default values
  preserve all existing tests). `Judge` gains `evaluate_full(summary,
  *, atv, inp)` with default fallback to `evaluate(summary)` —
  backward compatible.
* **`src/aegis/judge/__init__.py`** — `get_judge()` routes
  `attribution_head`, `local-phi`, and `hybrid` providers.
* **`src/aegis/firewall/step340_policy.py`** — calls `judge.evaluate_full(
  summary, atv=atv, inp=inp)` so M13-style judges get the structured
  signal. Backward compatible (legacy judges fall back to `evaluate`).
* **`src/aegis/config.py`** — `aegis_judge_provider` Literal extended
  with `attribution_head`, `hybrid`. New env vars:
  `AEGIS_JUDGE_MODEL_PATH`, `AEGIS_JUDGE_LOCAL_PHI_STUB`.

### Demo

* **`demo/judge_stack.py`** (new) — runs the same 5 canonical tool
  calls through both M13 alone and the v3.0 hybrid stack. Prints
  per-tier decision / confidence / latency + final verdict + reason.
  Live verified: every scenario decides at M13 (Tier 1) with
  cumulative latency <1 ms.

### Tests

* +56 unit tests (849 → 905 total). Coverage:
  * Attribution head (21): weights file SHA3, model_hash determinism,
    text fallback, evaluate_full populates 30-key map, latency, blast
    discrimination, destructive-arg → top contributor, HW anomaly →
    HW subfields in top-3, innocent read → ALLOW < 0.40, score clamping.
  * LocalPhiJudge (19, 1 skipped): mode detection (stub default,
    explicit stub, missing model → disabled), real-file SHA3 hash,
    stub block on destructive args + allow on innocent read, text-only
    fallback, deterministic same-input, _parse_real_decode JSON +
    keyword fallback + unparseable → ALLOW.
  * HybridJudge (16): default-layer construction with/without Anthropic
    key, BLOCK short-circuits, high-confidence ALLOW commits, low-
    confidence ALLOW escalates, REQUIRE_APPROVAL commits, fall-through
    to last tier, layer_traces / model_hash / cumulative latency, real
    default stack catches `rm -rf`, deterministic same-input.

### Verified gates

* `pytest -q`     → **905 passed** + 1 skipped (was 849).
* `mypy src`      → clean, **89 source files** (was 86).
* `ruff check .`  → clean.
* Live demo: 5 / 5 scenarios decided at Tier 1 in <1 ms aggregate.

### Migration from v2.4.x

No breaking changes. `aegis_judge_provider` defaults to `dummy` so the
existing surface is unchanged. To opt in to ATV-native judging:

```bash
export AEGIS_JUDGE_PROVIDER=attribution_head    # M13 only, fastest
# or
export AEGIS_JUDGE_PROVIDER=hybrid               # full stack with fallback
# Optional: real Phi-4-mini-q4
export AEGIS_JUDGE_MODEL_PATH=/path/to/phi-4-mini-q4.gguf
uv pip install llama-cpp-python
```

### What is NOT done

* Real Phi-4-mini-q4 model file is **not bundled** — multi-GB GGUF
  files don't fit in the repo. Stub mode covers the contract; real
  mode activates when the user downloads the model.
* M13 weights are **hand-tuned**, not learned. v3.x will replace with
  weights trained from labelled (ATV, verdict) pairs collected via
  the Burn-in Shadow phase (M11).
* Cross-hardware quantized determinism (Apple Metal vs CUDA vs CPU)
  is "attestable per (model, backend, hw)" — addressed by storing
  backend hash alongside `model_hash` in v3.x.

---

## [2.4.0] — 2026-04-28  ·  step337 HW band anomaly gate

Closes the gap surfaced by v2.3's demo (3 / 6 attacks unblocked).
Adds a new firewall step that reads the ATV HW band's normalized
signals and converts clear-cut anomalies into BLOCK / REQUIRE_APPROVAL
— complementing the M12 cost-divergence escalation (Claim 27) which
only watches the j-14/j-15/j-16 cost axis. Together M12 + step337
catch all 6 simulator attack modes.

### Added — `step337_hw_anomaly`

* **`src/aegis/firewall/step337_hw_anomaly.py`** — new firewall step
  reading the ATV HW band directly:

  Severity 1 (BLOCK):
  * ``aid_tag_transitions[0]`` ≥ 0.20 → ``rule:hw_iommu_violation``
    (IOMMU AID-tag breach, Claim 5 enforcement target).
  * ``atmu_anomaly[2]``        ≥ 0.34 → ``rule:hw_hypervisor_violation``
    (VM-to-host ring violation).
  * ``network_telemetry[0]``   ≥ 0.20 AND tool ∉ ``_EGRESS_ALLOWED``
    → ``rule:hw_network_exfil`` (≥10 MB egress on a non-egress tool).

  Severity 2 (REQUIRE_APPROVAL):
  * ``thermal_ecc_drift[3]`` == 1.0 → ``rule:hw_thermal_spike``
    (sustained ≥90°C p95).
  * ``dma_fanout[0]``        ≥ 0.50 → ``rule:hw_dma_fanout``
    (≥16 distinct DMA peers in one call).
  * ``atmu_anomaly[3]``      ≥ 1.0 → ``rule:hw_ecc_uncorrectable``
    (uncorrectable ECC error — possible rowhammer probe).

  ``_EGRESS_ALLOWED`` covers ``WebFetch``, ``WebSearch``, ``fetch``,
  ``http_request``, ``curl``, ``browse``, ``render_image``,
  ``embed_image``, ``send_email``, ``post_message``,
  ``call_external_api`` so step337 doesn't BLOCK the legitimate
  outbound use cases (step311's ``exfil_url`` rule still inspects
  the URL itself).

  Step short-circuits to no-op when the HW band is zero-filled
  (T2 default + sidecar without ``AEGIS_HW_PROVIDER=sim``), so the
  v2.0 / v2.2 / v2.3 surface is unaffected.

### Changed

* **`src/aegis/firewall/core.py`** — `default_steps()` inserts
  step337 between step336 (loop) and step340 (sLLM judge).
* **`_donor/smoke_hw_double_check.py`** — demo now runs both gates
  (M12 escalation + step337) and prints a 7-column matrix. All 6
  attack modes light up:

  ```
   scenario                      tok→FLOPS  mem    $    M12     s337   rule
   ──────────────────────────── ────────── ───── ──── ──────── ───── ───────────────
   ✓ honest agent (no attack)        0.05  0.08 0.05      ok    ok    -
   ✗ attack: cost_underreport         1.00  0.08 1.00 ESCALATE  ok    token_to_flops
   ✗ attack: hbm_exfil                0.05  1.00 0.05 ESCALATE  ok    memory_cost
   ✗ attack: iommu_violation          0.05  0.08 0.05      ok BLOCK   rule:hw_iommu_violation
   ✗ attack: network_exfil            0.05  0.08 0.05      ok BLOCK   rule:hw_network_exfil
   ✗ attack: thermal_spike            0.05  0.08 0.05      ok   ASK   rule:hw_thermal_spike
   ✗ attack: token_flops_mismatch     1.00  0.08 1.00 ESCALATE  ok    token_to_flops
   ✗ multi: token+network+iommu       1.00  0.08 1.00 ESCALATE BLOCK  token_to_flops
  ```

### Tests

* +20 unit tests (829 → **849** total). Coverage: T2 zero-band
  short-circuit, honest-path no-op, each severity-1 and severity-2
  rule fires individually, egress-allowed tools exempt from
  ``hw_network_exfil`` (parametrised across 6 tools), priority
  ordering (BLOCK before REQUIRE_APPROVAL, IOMMU before thermal),
  threshold edge case (0.19 just below 0.20 doesn't fire),
  ``ctx.extras`` audit hint contract, end-to-end through
  `run_firewall` (step337 BLOCK propagates to Verdict).

### Verified gates

* `pytest -q`     → **849 passed** (was 829).
* `mypy src`      → clean, **86 source files** (was 85).
* `ruff check .`  → clean.
* Live demo: every attack mode catches at one of two gates.

### Migration from v2.3.x

No breaking change. step337 is a no-op when the HW band is
zero-filled, which is the default unless ``AEGIS_HW_PROVIDER=sim``
is set. The user already-on-sim path picks up the new gate
automatically — no env var changes needed.

---

## [2.3.0] — 2026-04-27  ·  T3 hardware-emulation: SW/HW double-check live

Brings the patent's two-axis double-check model (Claims 26 / 27 / 30 / 34)
on-line *without* needing real T3 silicon (M19 RAPL/NVML, M20 FPGA, M21
IOMMU, M22 CSD all stay deferred). Adds a deterministic SW emulator
that populates the 200-D ATV HW band so the M12 cost-divergence
escalation gate becomes a working defence layer instead of a 0÷0
placeholder.

### Added — HW telemetry simulator

* **`src/aegis/hw_telemetry/`** (new package):
  * `simulator.py` — `HWCounters` dataclass + `simulate(inp, attack="")`
    function. Deterministic via SHA3-seeded `_seeded_floats(...)`
    keyed on (tool, args, aid). Honest path = SW-expected baseline
    (`expected_flops` from `cost.model_flops`) jittered ±10% so all
    three j-14 / j-15 / j-16 divergence metrics stay <0.30 escalation
    threshold.
  * `simulate_from_env(inp)` — env-driven on/off + attack injection.
    `AEGIS_HW_PROVIDER=sim` switches on; `AEGIS_HW_INJECT_ATTACK=...`
    rewrites counters to make the gate fire.
  * Six attack modes (the SW emulation of compromised silicon):
    * `token_flops_mismatch` — HW FLOPS = 50× SW expected → catches
      model substitution / hidden background work.
    * `hbm_exfil` — HBM bytes = 30× SW expected → side-channel via
      KV cache / activations.
    * `cost_underreport` — HW FLOPS = 100× while SW dollar stays the
      same → billing-evasion compromise.
    * `thermal_spike` — sustained 95°C p95 + ECC spikes.
    * `network_exfil` — 50 MB egress on a "read_file" call.
    * `iommu_violation` — IOMMU AID-tag breaches (Claim 5
      enforcement target).
* **`src/aegis/atv/hw_encoders.py`** (new) — 11 deterministic
  projections from `HWCounters` to ATV HW band subfields:
  `memory_timing_histograms` (32-D), `aid_tag_transitions` (24-D),
  `atmu_anomaly` (16-D), `dma_fanout` (16-D), `thermal_ecc_drift`
  (16-D), `watchdog_signals` (12-D), `network_telemetry` (24-D),
  `gpu_accelerator_state` (16-D), `hypervisor_signals` (8-D),
  `hw_cost_attestation` (16-D), `linkage_consistency` (20-D).
  Per Claim 26, slots 13/14/15 of `hw_cost_attestation` carry the
  j-14/j-15/j-16 divergence values directly so the cryptographic
  audit record is self-attesting.

### Changed

* **`src/aegis/atv/builder.py`** — `build_atv(inp, *, hw=None)` accepts
  an optional `HWCounters`. When absent (default) the HW band stays
  zero-filled (T2 contract); when present, the encoders fire and the
  HW band carries real signal.
* **`src/aegis/api/evaluate.py`** — calls `simulate_from_env(inp)`
  early, threads the result into both `build_atv(inp, hw=...)` and
  `compute_divergence(..., hw_flops_observed=hw.flops_observed,
  hw_hbm_bytes_observed=hw.hbm_bytes_observed)`. Existing M12
  escalation gate (Claim 27) now lights up under attack injection
  without any code change to the firewall pipeline.
* **`src/aegis/config.py`** — two new settings (default `none` /
  empty string so the v2.0/v2.1/v2.2 surface is unaffected):
  `aegis_hw_provider: Literal["none", "sim"] = "none"` and
  `aegis_hw_inject_attack: str = ""`.

### Tests

* +30 unit tests (792 + v2.2.1's 7 → 829 total). Coverage: simulator
  determinism, aid-isolated jitter, every attack mode produces an
  observable counter change, three "guaranteed escalation" attacks
  trigger the M12 gate, env-var on/off + attack pickup, every
  encoder's shape + clamp + slot semantics, builder integration with
  HW-cost-attestation slot 13 numerically equal to compute_divergence
  output.

### Demo

* `_donor/smoke_hw_double_check.py` — runs the same synthetic SW
  request through honest + each of 6 attack modes + a multi-attack
  combination, prints the `(token_to_flops, memory, dollar, gate,
  metric)` matrix. 3 / 6 attack modes (`token_flops_mismatch`,
  `hbm_exfil`, `cost_underreport`) trip the M12 cost-axis gate; the
  other 3 are visible in the ATV HW band (subfields populated as
  expected) but require their own firewall step for BLOCK conversion
  — clean roadmap for a future step `step337_hw_anomaly`.

### What is still NOT done

This is **SW emulation, not real T3**. The hardware procurement
roadmap is unchanged:

* M18 ML-DSA dual-signing (oqs-python) — pure SW, can land any time.
* M19 RAPL/NVML — needs Linux server + GPU.
* M20 FPGA sLLM — Xilinx Versal AI Edge VEK280.
* M21 HW tag comparator — bare-metal IOMMU.
* M22 CSD — Solidigm D7-PS1010 eval kit.

The simulator's `HWCounters` envelope matches the data shape M19–M22
will deliver, so the wire from `evaluate.py` → `compute_divergence`
→ M12 ledger is *already correct*. Replacing `simulate(...)` with a
real driver per provider is a one-file swap when silicon shows up.

### Verified gates

* `pytest -q`                                       **829 passed**
                                                     (was 799).
* `mypy src` — clean, **85 source files** (was 82).
* `ruff check .` — clean.
* HW band non-zero in audit records when `AEGIS_HW_PROVIDER=sim`.
* M12 escalation flips ALLOW → REQUIRE_APPROVAL on attack injection
  (verified live by `_donor/smoke_hw_double_check.py`).

### Migration from v2.2.x

No breaking change. Sidecar service installs continue to use HW
band = 0 unless `AEGIS_HW_PROVIDER=sim` is set in their environment.
For demos / dogfood:

```bash
docker compose down
echo 'AEGIS_HW_PROVIDER=sim' >> .env
echo 'AEGIS_HW_INJECT_ATTACK=token_flops_mismatch' >> .env  # optional
docker compose up -d
```

After this, every `/evaluate` request gets a populated HW band and
divergence-triggered REQUIRE_APPROVAL on the chosen attack mode.

---

## [2.2.0] — 2026-04-27  ·  must-install: Safe Auto-Run + Poisoned Instruction Detector

This release closes the "must-install" gap from the v2.0 strategy
review. Five v2.1 features (Safe Auto-Run, cloud destructive rules,
Loop Saver, Risk Report, local signed audit) plus the v2.2 Poisoned
Instruction Detector turn the sidecar / plugin into the
**"Aegis Guard makes Claude Code & Codex safe enough to run
unattended"** product.

### Added — v2.1 Safe Auto-Run + Cost saver + visibility

* **v2.1.1 Safe action allowlist** — new `step305_safe_allowlist`
  runs first in the pipeline. Curated `policies/safe_actions.json`
  flags read-only file tools (Read / Grep / Glob, ``any_args``) and
  60 bash subcommand prefixes (file inspection, formatters, test
  runners, read-only git) as ``ctx.extras["safe_fast_path"] = True``.
  step340 honors the flag and skips the sLLM judge round-trip,
  dropping median latency from ~150 ms (Haiku) to <5 ms.
  Disqualifying shell metachars (``|``, ``;``, ``&&``, ``>``, ``$()``,
  backticks) immediately revert the call to the full pipeline so a
  destructive subshell never papers over a safe leading verb.
* **v2.1.2 step311 cloud + sql_unbounded patterns** — kubectl
  delete / drain, terraform destroy / apply -auto-approve / state rm,
  aws s3 rm / iam delete-user / iam create-access-key / ec2
  terminate-instances / rds delete-db-*, gcloud iam roles | service-
  accounts delete + iam service-accounts keys create + compute | sql
  | kms ... delete + projects delete / remove-iam-policy-binding, az
  role assignment create | delete + vm | sql | storage | keyvault
  delete, helm uninstall | delete, docker rmi -f | system prune -a |
  volume rm. Plus DELETE / UPDATE without WHERE on sql-class tools
  (incl. bash-tunneled ``psql -c "DELETE FROM logs"``).
* **v2.1.3 Loop & Redundant Call Saver** — new
  `aegis.monitor.loop_detector` (per-session, lock-protected SHA3
  counter) + `step336_loop`. Loop = same (tool, args_hash) repeated
  ≥ 3 times → REQUIRE_APPROVAL. Redundant = read-only repeat within
  300 s window → ALLOW + ``ctx.extras["redundant"] = True`` so the
  risk report can later count "N redundant calls deduped".
* **v2.1.4 ``aegis report``** — 5-line Agent Risk Report that reads
  the local audit JSONL and bins by decision + reason:

  ```
  ✅  N safe tool calls auto-approved
  ⚠️   K high-risk actions required approval
  ⛔  B destructive commands blocked
  ⛔  P poisoned-instruction sources detected
  💸  D redundant calls deduplicated
  🔁  L potential loops aborted
  🧾  Full signed local audit: <path>
  ```

  ``--since 24h`` filters by ts_ns; ``--verbose`` adds a top-10
  reason × count table.
* **v2.1.5 Local-mode SHA3 audit chain** — every line in
  ``~/.aegis/audit.jsonl`` now carries ``prev_hash`` + ``this_hash``
  so any post-write mutation breaks every subsequent recompute.
  ``aegis verify-audit`` walks the chain end-to-end and reports the
  first broken record. Sidecar mode is unchanged (M5/M9/M15 Ed25519
  + Merkle + AES-GCM remain canonical there).

### Added — v2.2 Poisoned Instruction Detector

* **`src/aegis/instruction_baseline/`** — captures SHA3-256 hashes
  of CLAUDE.md, AGENTS.md, .mcp.json, .claude-plugin/plugin.json,
  .claude/skills/*.md, .claude/commands/*.md, .cursor/rules/*.mdc.
  ``snapshot``, ``diff_baseline``, ``write/load_baseline`` are pure
  stdlib; ``DriftReport(added, removed, modified)`` is the contract.
* **`step309_instruction_drift`** — sits after step305, before
  step310. Re-hashes on every PreToolUse and BLOCKs on any drift
  with reason ``instruction_drift: <summary> (<top-3-files>)``.
  Disabled by default (settings.aegis_instruction_baseline_path = ""
  → no-op) so existing sidecar tests pass unchanged.
* **`aegis baseline {init|status|reattest}`** — repo-local manifest
  management. Default path is ``.aegis/instruction_baseline.json``
  under the repo root. ``init`` refuses to overwrite without
  ``--force``; ``status`` exits 1 on drift with per-file diff;
  ``reattest`` overwrites and drops the firewall's in-process cache.

### Changed

* `src/aegis/firewall/core.py` `default_steps()` is now a 10-step
  pipeline:

  ```
  step305_safe_allowlist  (v2.1.1)
  step309_instruction_drift  (v2.2)
  step310_args
  step311_donor_rules  (D11 + v2.1.2 cloud)
  step312_normalize
  step315_aid_auth
  step320_blast
  step330_human
  step335_cost
  step336_loop  (v2.1.3)
  step340_policy  (skips judge when safe_fast_path is set)
  ```

* `tests/conftest.py` `aegis_app` fixture resets the module-level
  default loop detector before and after each test so cross-test
  bleeds (the existing burnin e2e re-posts the same call 5×) don't
  trigger spurious loop verdicts.

### Tests

* +142 unit tests (Phase 0 baseline 455 → v2.0.0 650 → **v2.2.0 792**).
  Coverage: 23 step305, 38 step311 cloud rules, 22 loop detector +
  step336, 7 ``aegis report``, 17 local audit chain + verify-audit, 16
  instruction baseline, 8 step309, 9 ``aegis baseline``.

### Verified gates

* `pytest -q`                                       **792 passed**.
* `mypy src` — clean, **82 source files**.
* `ruff check .` — clean.

### Migration from v2.0.x

No breaking changes for sidecar mode — step305 / step309 / step336
are no-op when disabled, and the new policies/safe_actions.json is
purely additive. To opt into the new surface in your install:

```bash
# v2.1 features ship enabled (safe allowlist + loop detector run by default).
# v2.2 baseline is opt-in:
uv run aegis baseline init                         # write the manifest
export AEGIS_INSTRUCTION_BASELINE_PATH=$(pwd)/.aegis/instruction_baseline.json
# Restart the service / Claude Code.
```

---

## [2.0.0] — 2026-04-26  ·  aegis-mvp plugin merged into T2 sidecar

This release merges the `aegis-mvp v1.0.0` Claude Code plugin (142
files, 62 tests) into the existing AegisData T2 sidecar (M1–M17, 455
tests). The result is a **single codebase, two deployment modes**,
sharing one ATV / ATMU (Agent Telemetry Management Unit) / Burn-in core:

* **Sidecar mode** (default) — multi-tenant FastAPI; the host hook
  POSTs to ``localhost:8000/evaluate``. Audit signing, cost ledger,
  HAM and Burn-in are the full M1–M17 surface.
* **Plugin (`local`) mode** (new) — single-developer in-process hook;
  no service, no HTTP, no API keys. Solo Free tier.

### Added — plugin surface (D1–D6)

* **D1** — `tools/aegis_payload.py`: Claude Code ↔ ``/evaluate``
  payload adapter. Normalises both Claude Code's ``PreToolUse`` shape
  (``session_id`` / ``tool_name`` / ``tool_input``) and the legacy
  ``{tool, args, agent_id}`` shape; maps internal verdicts
  (``allow`` / ``block`` / ``require_approval``) onto Claude Code's
  ``hookSpecificOutput.permissionDecision`` (``allow`` / ``deny`` /
  ``ask``).
* **D2** — `.claude-plugin/plugin.json` v2.0.0 manifest (PreToolUse +
  PostToolUse + Stop hooks, six sprint-N-kickoff slash commands, the
  ``aegis-mvp`` skill, and the ``tier`` / ``policy_pack`` /
  ``burnin_baseline`` / ``sllm_endpoint`` config schema).
* **D3** — `tools/aegis_cli.py`: ``aegis`` CLI with 14 subcommands
  (``status`` / ``verify-audit`` / ``replay`` / ``policy-replay`` /
  ``cost`` / ``health`` / ``rollback`` / ``snapshots`` / ``burnin`` /
  ``cost-record`` / ``cost-import`` / ``budget`` / ``install``).
  Promoted ``tools/`` to a wheel package and added
  ``[project.scripts] aegis = "tools.aegis_cli:main"`` so
  ``uv run aegis install`` works after a fresh ``uv sync``. Absorbs
  the safety properties of the legacy ``tools/install_hook.py``.
* **D4** — `src/aegis/rollback/` + four strategies (file / shell /
  git / mcp). Pre-tool snapshot captures filesystem + git state so
  ``aegis rollback INVOCATION_ID`` can restore. Bulk restore via
  ``--session SID`` or ``--since ISO``.
* **D5** — `src/aegis/cost/transcript.py`: Claude Code transcript
  ``.jsonl`` parser. ``parse_transcript`` is pure;
  ``import_into_wal`` calls a pluggable ``ledger_writer`` hook
  (defaults to a parse-only no-op so no OPENAI/ANTHROPIC key is
  required — Phase 5 packaging rebinds it to the M12
  CostAttestationLedger).
* **D6** — `tools/hooks/session_end.py`: Claude Code Stop-event hook
  that auto-imports transcript cost data through D5 when a session
  ends.

### Added — ATV-2080 adapter (Phase 3)

* **`src/aegis/atv/adapter.py`** — `from_claude_code_payload(req, *,
  tenant_id, role_id, agent_state_text, plan_text) -> ATVInput`.
  Bridges the plugin payload shape into MVP/'s 30-subfield
  ATV-2080-v1 so the same ``/evaluate`` endpoint serves both modes.
  Trace IDs derived from invocation_id via SHA3-256 so re-evaluating
  the same call yields the same audit anchor.
* `donor_behavior_features(tool, args)` preserves the donor's 32-D
  hand-engineered feature vector verbatim for callers that want
  deterministic donor-style features.

### Added — donor pattern rule pack (D11, partial)

* **`src/aegis/firewall/step311_donor_rules.py`** — new firewall
  stage between step310 and step312, ports seven stdlib pattern
  rules from `_donor/aegis-mvp/atmu/rules/` that close the eight
  Phase 3 e2e gap incidents:
  * `persona_drift`     I-01  REQUIRE_APPROVAL — system-prompt
    extraction patterns ("repeat your system prompt").
  * `exfil_url`         I-04 / I-07  BLOCK — base64 / hex / long-query
    URL blobs and suspicious TLDs (`.tk` `.ml` `.ga` `.cf` `.gq`
    `.pw` `.top`) on egress tools (fetch / render_image / send_email).
  * `sandbox_escape`    I-06  BLOCK — `docker.sock`,
    `docker run --privileged`, `--cap-add=SYS_ADMIN`, `nsenter`,
    `mount --bind /`.
  * `prompt_injection`  I-08  REQUIRE_APPROVAL — "ignore previous
    instructions" patterns on input-bearing tools (fetch / read_file
    / search / rag_query / browse / read_page).
  * `mcp_injection`     I-09  BLOCK — instruction patterns inside
    newly-registered MCP tool descriptions.
  * `git_destructive`   I-10  BLOCK — `git push --force` to
    main / master / prod, `git branch -D main`, `git rebase main`.
  * `payment_overflow`  I-11  BLOCK — per-tool USD ceilings:
    stripe_charge ≥$1k, wire_transfer ≥$10k, ach_payment ≥$5k,
    crypto_send ≥$500, payout ≥$5k.
* `cost_overflow` and `malfunction_pattern` rules deferred to v2.1
  (depend on D7 ``monitor.malfunction`` and D10 ``cost.budget``,
  not yet ported).

### Added — plugin packaging (Phase 5)

* **`aegis install --mode {sidecar,local}`**:
  * `--mode sidecar` (default) — registers ``tools/aegis_hook.py`` so
    the hook POSTs to ``localhost:8000/evaluate``. Requires
    ``docker compose up -d``.
  * `--mode local` — registers ``tools/aegis_local_hook.py`` so the
    firewall pipeline runs in-process. Auto-prepends
    ``AEGIS_EMBEDDING_PROVIDER=dummy``,
    ``AEGIS_JUDGE_PROVIDER=dummy``, ``AEGIS_POLICY_DIR=…`` and
    ``PYTHONPATH=…`` so the spawned subprocess works without any
    OpenAI / Anthropic key (Solo Free contract per CLAUDE.md
    "Dummy/Mock Mode").
* **Plugin manifest validation** before install — refuses if
  ``.claude-plugin/plugin.json`` is missing, malformed, or lacks
  ``name`` / ``version``.
* **Stop hook auto-registration** alongside PreToolUse, idempotently
  across modes; sidecar + local entries can coexist (different
  markers).
* **Legacy migration banner** — when an ``install_hook.py`` entry is
  detected in the user's settings, prints a yellow note pointing at
  the new CLI but leaves the legacy line in place (preserves v1.x
  compatibility).

### Added — tests

* **+195 tests** (Phase 0 baseline 455 → 650).
  * Plugin / CLI: payload adapter (9), ``aegis`` CLI argparse +
    install (51), Stop hook (6), local hook smoke (11).
  * Rollback: 4 strategies + snapshot orchestrator (30).
  * Cost transcript parser (10).
  * ATV adapter + donor encoder features (27).
  * Donor rule pack (37).
  * 12-incident e2e through real ``/evaluate`` (14, 12 strict pass).

### Changed

* `src/aegis/firewall/core.py` — `default_steps()` now inserts
  `step311_donor_rules.run` between step310 and step312.
* `src/aegis/cost/__init__.py` — re-exports `parse_transcript` and
  `import_into_wal`.
* `pyproject.toml`:
  * `tools/` promoted to a hatch wheel package.
  * `[project.scripts] aegis = "tools.aegis_cli:main"` entry point.
* `INTEGRATION_PLAN.md` committed at the start of the merge as the
  living plan.

### Migration from v1.x

Existing `tools/install_hook.py` users can keep using it; the new
``aegis install`` CLI lands its own PreToolUse entry alongside the
legacy one and prints a yellow banner. To switch:

```bash
# 1. Pull v2.0
git pull && uv sync

# 2. Re-install with the new CLI
uv run aegis install --mode sidecar    # multi-tenant default
# or
uv run aegis install --mode local      # Solo Free, no service

# 3. (Optional) Remove the legacy install_hook.py entry from
#    ~/.claude/settings.json by hand.

# 4. Restart Claude Code.
```

### Verified end-to-end

* `pytest -q`                                         **650 passed**.
* `mypy src` — clean, **74 source files**.
* `ruff check .` — clean.
* `bash demo/scenarios/run_all.sh` — **7/7 PASS** in 68s.
* `/evaluate` against the 12-incident donor KPI panel —
  **12/12 strict** (4 via existing MVP rules, 8 via step311 D11).

### Deferred to v2.1

* D7 `src/aegis/monitor/malfunction.py` — runtime malfunction
  classifier (per-session error_rate / atv_loop / schema_drift).
* D8 `src/aegis/burnin/retrain.py` — sanity-check + revert wrapper
  around the M11 5-layer Burn-in baseline.
* D9 `src/aegis/api/replay.py` extension — policy-replay engine
  on top of the existing ``/forensic/replay`` endpoint.
* D10 `src/aegis/cost/budget.py` — hot-reloadable budget thresholds.
* `cost_overflow` and `malfunction_pattern` rules in step311 (depend
  on D10 / D7 above).
* `aegis status` / `aegis health` / `aegis policy-replay` /
  `aegis budget` / `aegis cost` — depend on D7–D10 backings; the
  CLI subcommands ship as lazy-imported stubs.

---

## [1.x] — pre-v2.0

The full pre-v2.0 milestone history (M1 FastAPI through M17 TEE
attestation, plus DOGFOOD Phase A/B and the 49-page WHITEPAPER) lives
in `git log` and `SESSION_HANDOFF.md` §4. This file covers v2.0
forward.
