# Architecture & Surface Tour

This document is the per-milestone reading guide. For each milestone
M8–M16 it lists:

* **What** the milestone introduces
* **Patent reference** — the section / claim / paragraph it implements
* **Files** that hold the implementation
* **Data flow** — which other parts of the system call in / out
* **External surface** — endpoints, env vars, on-disk artifacts
* **What's still T2-software** vs. what becomes hardware in T3

For the full claim coverage matrix, see [`PLAN_v2.md` §7](../PLAN_v2.md).
For the original 7-day M1–M7 design, see [`PLAN.md`](../PLAN.md).

---

## High-level data flow

```
host (agent runtime)
  │  POST /evaluate { ATVInput }
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  aegis.api.evaluate                                                 │
│    1. atv = build_atv(inp)             [aegis.atv.builder]          │
│    2. ctx = FirewallContext(atv, inp)                               │
│    3. for step in [310, 315, 320, 330, 335, 340]:                   │
│         result = step.run(atv, inp, ctx)                            │
│         if result.decision: stop                                    │
│    4. step350_approval.dispatch(ctx)   [if REQUIRE_APPROVAL]        │
│    5. step360_audit.sign_and_append(ctx)                            │
│         ├─ aegis.sign.ed25519                                       │
│         ├─ aegis.audit.sqlite_store    (transactional, chained)     │
│         ├─ aegis.audit.jsonl_store     (append-only)                │
│         └─ aegis.audit.encrypted_journal  [M15: AES-256-GCM]        │
│    6. step370_exec.annotate(ctx)        (PROCEED / SUPPRESS / DEFER)│
│    7. atmu.intent_log.append_tentative  [M10]                       │
│    8. cost_ledger.append                [M12, separate Ed25519 key] │
│    9. burnin.observe(ctx)               [M11]                       │
└─────────────────────────────────────────────────────────────────────┘
  │  Verdict { decision, reason, atv_id, signature, step_traces }
  ▼
host runs (or skips) the tool, then POSTs /tool-outcome to close the
ATMU intent record [M10].

Out-of-band:
  • GET /forensic/replay walks the encrypted journal             [M15]
  • GET /admin/aid lists per-AID quarantines                     [M14]
  • POST /ham/* manages agent's encrypted memory                 [M16]
```

---

## M8 — ATV-2080-v1 30-subfield schema

**Patent**: Appendix A; Claims 6, 7, 9, 24

**Files**
* [`src/aegis/schema.py`](../src/aegis/schema.py) — slice constants
  (header 0..63, agent_state 64..575, …, hw_band 1880..2079),
  `ATVHeader`, `ATVInput`, `Verdict`, `CostEfficiencyMetrics` (16 named
  slots s-1..s-16 mapped to ATV indices 1864..1879)
* [`src/aegis/atv/builder.py`](../src/aegis/atv/builder.py) — 19
  software encoders (text-embed for plan/state, hash-expand for ids,
  feature-extract for safety flags, zero-fill for HW band, etc.)

**Schema invariants**
* Total dimensionality is exactly 2080. `_assert_schema_valid()` runs at
  import time and refuses to load if any slice is non-contiguous or the
  total drifts.
* The 200-D HW band (indices 1880..2079) is always zero in T2. Its
  internal layout is reserved (`hw_cost_attestation` 2044..2059,
  `linkage_consistency` 2060..2079) so T3 can fill it without changing
  the external contract.

**External shape change** — M8 broke the `cost_estimate` body. Old
shape `{exp_bytes_write, exp_dollars, confidence}` was silently
dropped by Pydantic; new shape is the full `CostEfficiencyMetrics`
16-slot model. Most callers only fill a handful of slots
(`input_token_count`, `cumulative_dollars`,
`forecasted_cost_to_completion`); the rest default to 0.

---

## M9 — Firewall split 350 / 360 / 370

**Patent**: §[0061]–[0063]; Claims 2, 16

Original M1–M7 collapsed approval, audit, and exec annotation into the
`/evaluate` handler. M9 hoists each into a dedicated module so they
can be tested, reordered, and instrumented independently.

**Files**
* [`step350_approval.py`](../src/aegis/firewall/step350_approval.py) —
  `dispatch(ctx, channel='stderr'|'silent')`, plus `set_channel` /
  `drain_emitted` test helpers. Channel-pluggable so a future Slack /
  pager-duty notifier slots in cleanly.
* [`step360_audit.py`](../src/aegis/firewall/step360_audit.py) —
  `sign_and_append(verdict, atv, inp, ...)` consolidates the
  Ed25519-sign + sqlite-store + jsonl-store + encrypted-journal write
  + `cost_attestation_hint` boolean. Single transaction boundary.
* [`step370_exec.py`](../src/aegis/firewall/step370_exec.py) —
  `annotate(verdict)` adds an exec recommendation
  (`PROCEED` / `SUPPRESS` / `DEFER`) to `step_traces` so the host can
  decide at a glance.

---

## M10 — ATMU + Write-Ahead Intent Log + 2PC

**Patent**: §[0063H]–[0063J]; Claims 2, 15

The ATMU brackets every tool call in a Write-Ahead Intent record that
transitions through 7 explicit states. The host posts back via
`POST /tool-outcome` after running (or compensating) the tool.

**Files**
* [`atmu/state_machine.py`](../src/aegis/atmu/state_machine.py) —
  `TxState` StrEnum (`tentative`, `prepared`, `committed`, `aborted`,
  `rolled-back`, `compensated`, `quarantined`), `can_transition()` /
  `ensure_transition()`, `InvalidTransitionError`.
* [`atmu/intent_log.py`](../src/aegis/atmu/intent_log.py) —
  SQLite-backed Write-Ahead log with monotonic `seq`. Constructs the
  return dict synchronously (avoids a `:memory:` SQLite race seen in
  early development).
* [`atmu/checkpoint.py`](../src/aegis/atmu/checkpoint.py) —
  `make_checkpoint(inp, blast_radius)` returns a checkpoint manifest
  for blast≥7 (high-impact tools).
* [`atmu/compensating.py`](../src/aegis/atmu/compensating.py) —
  `DEFAULT_COMPENSATION_STRATEGIES` table mapping tool names to
  rollback / no-op / human-review strategies.
* [`api/tool_outcome.py`](../src/aegis/api/tool_outcome.py) —
  `POST /tool-outcome { record_id, status, result_hash, ... }`.

**State machine** (illegal transitions return HTTP 409):

```
            ┌──────────┐
            │tentative │  (created at /evaluate time)
            └────┬─────┘
                 │  ALLOW or REQUIRE_APPROVAL
                 ▼
            ┌──────────┐
            │ prepared │  (audit record signed + appended)
            └────┬─────┘
                 │
        success  │  failure / timeout / partial
        ──────┐  │  ┌──────────────────
              ▼  ▼  ▼
       ┌─────────┐ ┌─────────┐ ┌────────────┐ ┌─────────────┐
       │committed│ │ aborted │ │rolled-back │ │ compensated │
       └─────────┘ └─────────┘ └────────────┘ └─────────────┘

  any state ─→ quarantined  (M14 circuit breaker may force this)
```

---

## M11 — 5-layer Burn-in × 4-phase graduation

**Patent**: §7 / ¶[0075]; Claims 4, 13, 14, 19, 20

Statistical baseline maturity, **not** code attestation (which is
M7's `aegis.attest.code_attestation` — these are different things
that are sometimes both called "burn-in" in the patent).

**Layers** (each is a separately-tracked slot per layer key):

| Layer | Key shape | Default expected samples |
|---|---|---|
| L1 hardware | `"L1"` | 1000 |
| L2 tenant | `"L2:{tenant_id}"` | 5000 |
| L3 topology | `"L3:{tenant_id}"` (extends in T3 with cluster id) | 2000 |
| L4 role | `"L4:{tenant_id}:{role_id}"` | 1000 |
| L5 instance | `"L5:{tenant_id}:{role_id}:{aid}"` | 500 |

**Phases**: `observation → shadow → assisted → production`. Graduation
gates per `aegis.burnin.phases.can_graduate`:

1. `samples ≥ expected_samples`
2. `tpr ≥ 0.95 AND fpr ≤ 0.02 AND precision ≥ 0.90`
3. `override_rate ≤ 0.05`

**Files**
* [`burnin/phases.py`](../src/aegis/burnin/phases.py) — `Phase`
  StrEnum, `PhaseMetrics` dataclass with derived TPR/FPR/precision
  properties, `can_graduate()` gate.
* [`burnin/controller.py`](../src/aegis/burnin/controller.py) —
  `BurnInController` with `observe()`, `record_label()`,
  `try_graduate()`, `event_*()`, `composite_score()`, `status()`.
* [`api/burnin_status.py`](../src/aegis/api/burnin_status.py) — three
  endpoints.

---

## M12 — Cost Attestation Ledger

**Patent**: §12; Claims 3, 26, 27, 30, 33, 34

Separate signed store for cost records, with its own Ed25519 key
(Claim 34 — distinct from the telemetry signing key). Three
divergence metrics flag agents whose actual usage is decoupled from
the model the firewall thinks they're running.

**Files**
* [`cost/model_flops.py`](../src/aegis/cost/model_flops.py) —
  `FLOPS_PER_TOKEN` table per model, `expected_flops()`,
  `expected_dollars()`.
* [`cost/divergence.py`](../src/aegis/cost/divergence.py) —
  `DivergenceMetrics` (3 ratios: token-to-FLOPs, memory-cost,
  dollar-cost), `_safe_relative()` helper, `compute_divergence()`. All
  three are SW-normalized for consistency.
* [`cost/escalation.py`](../src/aegis/cost/escalation.py) —
  `EscalationDecision`, `evaluate_escalation()` with the
  3× baseline threshold from Claim 27. Independent of sLLM verdict.
* [`cost/ledger.py`](../src/aegis/cost/ledger.py) —
  `CostAttestationLedger` with own key, sqlite + jsonl, per-aid Merkle
  chain, `verify_chain()`.
* [`api/cost_attestation.py`](../src/aegis/api/cost_attestation.py) —
  `GET /cost-attestation/{aid}` and `/by-tenant/{tenant_id}`.

**Storage**: `./data/cost_attestation.sqlite` + `.jsonl`,
keys at `./keys/ed25519_cost.{pem,pub}`.

---

## M13 — sLLM attribution head

**Patent**: ¶[0066]; Claims 8, 11

Patent calls for a small (0.1–1B parameter) sLLM with three output
heads: verdict, confidence, and **attribution** (per-subfield
contribution scores). T2 keeps Claude Haiku 4.5 as the verdict head
but adds an attribution requirement to the system prompt.

**Files**
* [`judge/base.py`](../src/aegis/judge/base.py) — `JudgeVerdict` gains
  `subfield_attribution: dict[str, float]`.
* [`judge/haiku.py`](../src/aegis/judge/haiku.py) — `SYSTEM_PROMPT`
  requires an `"attribution"` JSON object listing the 30 ATV-2080-v1
  subfields it weighted; `max_tokens` raised 200 → 400 to fit.
* [`firewall/step340_policy.py`](../src/aegis/firewall/step340_policy.py)
  — trace string includes the top-3 attributed subfields so the
  audit record carries them through to the dashboard / theater view.

T3 will swap the Haiku call for a pinned 0.1–1B model on FPGA/AIE
(Claim 11 — bit-exact deterministic inference). The interface is the
same so the swap is mechanical.

---

## M14 — AID auth + per-AID circuit breaker

**Patent**: §5B / ¶[0063L]–[0063M]; Claim 5B

Per-AID (agent identifier) authorization table + violation counter +
auto-quarantine. T2 software emulation; T3 puts the same logic in
the hardware tag comparator at the memory controller (¶[0063K]).

**Files**
* [`firewall/circuit_breaker.py`](../src/aegis/firewall/circuit_breaker.py)
  — `AidStatus` StrEnum, `AidState` dataclass, `CircuitBreaker` with
  thread-safe `record_violation` / `is_quarantined` / `release`.
  Snapshot-under-lock pattern (`_snapshot_locked()`) to avoid the
  re-entrant-Lock deadlock that bit early development.
* [`firewall/step315_aid_auth.py`](../src/aegis/firewall/step315_aid_auth.py)
  — `lru_cache`-loaded policy, `_aid_key()` = `tenant_id:role_id`,
  hard-block-if-quarantined check before allowed_tools / allowed_paths
  whitelists.
* [`policies/aid_region.json`](../policies/aid_region.json) —
  permissive default (`allowed_tools: []` = no restriction) plus two
  example strict roles (`read-only-role`, `financial-role`).
* [`api/admin_aid.py`](../src/aegis/api/admin_aid.py) —
  `GET /admin/aid`, `GET /admin/aid/{aid}`, `POST /admin/aid/release`
  with `X-Aegis-Admin-Token` header.

**Threat model**: a runaway agent that's been compromised or
mis-configured will trip its AID's violation counter. Auto-quarantine
freezes future tool calls from that AID at step 315 — before any
expensive sLLM call — until an admin releases it. The release path
requires the admin token (T2 stand-in for a signed administrative
recovery policy / human approval, per the patent).

---

## M15 — AES-256-GCM encrypted journal + forensic replay

**Patent**: §13B / ¶[0102G-1]

Every audit record is also written to an AEAD journal so a tampered
file is detected at decrypt time, not at audit-display time. The
cleartext header — schema_version, key_version, tenant_id, aid,
atv_commitment, ts_ns — is used as additional-authenticated-data, so
flipping any of those bits also fails the auth tag.

**Files**
* [`audit/encrypted_journal.py`](../src/aegis/audit/encrypted_journal.py)
  — `EncryptedJournal.append/iter`, AES-256-GCM with 12-byte random
  nonce, structured cleartext header.
* [`audit/replay.py`](../src/aegis/audit/replay.py) — `ReplayReport`
  dataclass; `replay()` walks the journal and rebuilds the per-AID
  prev-hash chain. Surfaces `tampered_records` + `tampered_count` +
  `per_aid_chain_valid`.
* [`api/replay.py`](../src/aegis/api/replay.py) —
  `GET /forensic/replay`. Read-only, safe to call repeatedly.

**Storage**: `./data/journal.bin` (binary length-prefixed records),
data-key at `./keys/journal_data.key`. The data-key is auto-generated
on first run if missing — for production, mount it from a secrets
manager.

**Why a separate journal?**: the audit SQLite is optimized for live
chain walks (per-AID indexed reads). The journal is optimized for
forensic replay (sequential reads, AEAD per record). They're written
in the same step-360 transaction so they can't drift.

---

## M16 — Hierarchical Agent Memory L3+L4

**Patent**: §13A / ¶[0102C]

The patent describes 4 levels — L1 register-cache, L2 NVMe-tier,
L3 object-store, L4 cold-archive. T2 emulates **L3+L4** with an
encrypted SQLite store + an in-process L1 OrderedDict cache.
L2 (NVMe) is reserved for T3.

**Six operations** (patent §13A):

| Op | Endpoint | What it does |
|---|---|---|
| `memory` | `POST /ham/memory` | Store an opaque dict; AES-256-GCM with AAD bound to `(tenant_id|aid|seq)` |
| `recall` | `POST /ham/recall` | Most-recent-N retrieval with optional tag filter, decrypted on demand |
| `context` | `POST /ham/context` | Bundle the N most-recent items + their object_ids for prompt assembly |
| `forget` | `POST /ham/forget` | Tombstone (idempotent); returns `false` if the object_id never existed for the AID |
| `summarize` | `POST /ham/summarize` | Counts + tag histogram |
| `ground` | `POST /ham/ground` | Bind a claim to N memory references → SHA3 `claim_hash` + resolved subset + missing list |
| `stats` | `GET /ham/stats` | Diagnostic counts (total / live / tombstoned) |

**Files**
* [`ham/store.py`](../src/aegis/ham/store.py) —
  `HierarchicalMemoryStore` with single threading.Lock,
  snapshot-under-lock pattern, L1 cache via `OrderedDict` capped at 256.
* [`api/ham.py`](../src/aegis/api/ham.py) — Pydantic models for each
  request, 7 endpoints.

**Encryption invariant**: tampering with any of `tenant_id`, `aid`, or
`seq` causes AAD verification to fail at decrypt — the cleartext index
holds tags + tombstone state only, never the body. A test in
`tests/unit/test_ham.py::TestEncryption::test_tampered_aad_rejected`
asserts this.

---

## Cross-cutting: signing keys

| Key file | Used by | Why distinct |
|---|---|---|
| `keys/ed25519.{pem,pub}` | `aegis.sign.ed25519` (telemetry: audit chain, `/attestation`, `/approve`) | Default Ed25519 telemetry key |
| `keys/ed25519_cost.{pem,pub}` | `aegis.cost.ledger` | **Claim 34**: cost attestation must be signable / disclosable independently of telemetry |
| `keys/journal_data.key` | `aegis.audit.encrypted_journal` | AES-256 symmetric data-key (32 bytes raw) — never signs, only encrypts |
| `keys/ham_data.key` | `aegis.ham.store` | AES-256 symmetric data-key for HAM bodies |

All four files are auto-created on first boot if missing. For
production, override the paths via env vars and mount them from a
secrets manager (Vault, AWS Secrets Manager, etc.).

---

## Cross-cutting: storage

| Path | Format | Written by | Read by |
|---|---|---|---|
| `data/audit.sqlite` | SQLite WAL | step360 | `/audit/{aid}`, dashboard |
| `data/audit.jsonl` | NDJSON | step360 | offline replay tools |
| `data/intent_log.sqlite` | SQLite WAL | ATMU | `/tool-outcome`, internal |
| `data/cost_attestation.sqlite` | SQLite WAL | step360 (when `cost_attestation_hint`) | `/cost-attestation/{aid}` |
| `data/cost_attestation.jsonl` | NDJSON | step360 | offline replay |
| `data/journal.bin` | length-prefixed AEAD records | step360 | `/forensic/replay` |
| `data/ham.sqlite` | SQLite WAL | `/ham/memory` | `/ham/{recall,context,...}` |

**Backup target**: snapshot `data/` + `keys/` together — the SQLite
DBs are in WAL mode, so a `sqlite3 .backup` is point-in-time
consistent without a service stop.

---

## What stays software in T2 vs. moves to hardware in T3

| Surface | T2 (this MVP) | T3 (patent-future) |
|---|---|---|
| ATV signing | Software Ed25519 | TEE-attested signing inside enclave |
| Code attestation | Hash of source files at startup | MRENCLAVE-style hardware attestation |
| AID-tag enforcement | step315 software middleware | HW tag comparator at memory controller |
| sLLM judge | Claude Haiku 4.5 (network) | Pinned 0.1–1B model on FPGA/AIE (deterministic) |
| Cost attestation | Software measurement | Hardware perf counter readout (`hw_cost_attestation` 2044..2059) |
| HAM L1+L2 | In-process OrderedDict (L1) only; L2 reserved | Hardware register cache (L1) + CXL NVMe pool (L2) |
| Encrypted journal | AES-256-GCM in software | Same algorithm, TEE-bound key |

The external contract — the schema, every endpoint, every JSON shape —
is held constant across the boundary. T3 is a substitution exercise,
not a rewrite.

---

## Reading order for a new contributor

1. [`PLAN.md`](../PLAN.md) — original 7-day MVP design (M1–M7). Skip the
   per-day timeline; read §6.x for the architecture justification.
2. This file (`docs/ARCHITECTURE.md`) — for the patent-aligned M8–M16
   surface.
3. [`PLAN_v2.md`](../PLAN_v2.md) §0 (one-page diff) and §7 (claim
   coverage matrix).
4. [`docs/QUICKSTART.md`](QUICKSTART.md) — get the service up and a
   verdict back in 60 seconds.
5. Pick a milestone you care about and read the listed files in the
   order above. Each milestone's tests live alongside it
   (`tests/unit/test_{module}.py`) and are the best executable
   documentation.
