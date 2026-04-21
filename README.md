# AegisData T2 MVP

A Python sidecar that wraps every AI-agent tool call in a 2,080-dimensional
**Agent Trace Vector (ATV-2080-v1)**, runs it through a 7-stage Action
Firewall, brackets the call in an **Agent Transaction Management Unit
(ATMU)** Write-Ahead Intent Log, asks an sLLM judge when needed,
**Ed25519-signs** every record, chains it into a **Merkle-linked
audit log**, and feeds every observation into a **5-layer Burn-in
controller** that progresses through Observation → Shadow → Assisted →
Production.

Implements the T2 (software-only) tier of AegisData provisional patent
v7.10. The original 7-day MVP design is in [`PLAN.md`](PLAN.md); the
patent-aligned re-plan and milestone status is in
[`PLAN_v2.md`](PLAN_v2.md).

---

## What's in the box

| Layer | Module | Notes |
|---|---|---|
| Schema | `aegis.schema` | ATV-2080-v1, **30 subfields** per patent Appendix A; pydantic v2 models |
| Vector | `aegis.atv.{builder, embeddings}` | OpenAI / dummy embedding backends; 19 SW encoders + HW zero-fill (T2) |
| Firewall (310..340) | `aegis.firewall.{core, step310..step340}` | regex deny-list / blast / human / cost / policy+sLLM |
| Firewall (350..370) | `aegis.firewall.{step350_approval, step360_audit, step370_exec}` | approval dispatch / sign+append / exec annotation |
| ATMU | `aegis.atmu.{state_machine, intent_log, checkpoint, compensating}` | Write-Ahead Intent Log + 2-phase commit + 7 transaction states + compensation plans |
| Burn-in | `aegis.burnin.{phases, controller}` | 5 layers (HW/tenant/topology/role/instance) × 4 phases with patent ¶[0075] graduation gates |
| Judge | `aegis.judge.{base, haiku, dummy}` | Claude Haiku 4.5; dummy fallback when no key |
| Sign | `aegis.sign.{ed25519, merkle}` | PEM PKCS8, canonical-JSON SHA3-256 + Merkle chain |
| Audit | `aegis.audit.{sqlite_store, jsonl_store}` | WAL + lock + `BEGIN IMMEDIATE` |
| Code attestation | `aegis.attest.code_attestation` | L3/L4/L5 code+config+key hash signed at startup; surfaced via `/attestation` |
| Pre-LLM safety | `tools/aegis_safety.py` (stdlib) | regex / OpenAI Moderations / Haiku classifier |
| API | `aegis.main`, `aegis.api.*` | see endpoint table below |

Hardware band (200-D, indices 1880..2079) is intentionally zero-filled in
T2 per patent ¶[0042] — that's the T3 work.

### Endpoints

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/healthz` | `{ok, version, burn_in_id}` | liveness + current code-attestation hash |
| POST | `/evaluate` | `Verdict` (decision/reason/atv_id/signature/step_traces) | full firewall + ATMU intent + audit append in one call |
| POST | `/approve` | `{ok, atv_id, head}` | record human approval as a chained signed record |
| POST | `/tool-outcome` | `{ok, record_id, current_state, tool_outcome}` | post-release outcome for an ATMU intent (M10) |
| GET | `/audit/{aid}` | `{aid, head, length, chain_valid, chain_error, chain}` | full Merkle-chained signed records for one agent |
| GET | `/attestation` | code-attestation measurement (L3/L4/L5 hashes + Ed25519 sig) | server software identity |
| GET | `/burnin-status` | `{layers[…], expected_samples, weights}` | live Burn-in baseline maturity (M11) |
| POST | `/burnin/graduate` | `{ok, layer_key, reason}` | manual phase advancement (409 if gates fail) |
| POST | `/burnin/label` | `{ok}` | feed labelled ground-truth into TPR/FPR/precision counters |
| GET | `/source/list` + `/source/peek` | code snippets | dashboard-only — used by Theater "Source-code paths" panel |

---

## Quick start

```bash
# 1. Install deps (downloads Python 3.11 if missing)
uv sync

# 2. Run the test suite
uv run pytest -q

# 3. Lint + typecheck
uv run ruff check . && uv run mypy src

# 4. Boot the service
uv run uvicorn aegis.main:app --reload --port 8000

# 5. In a second shell — run the 5-call demo against it
uv run python -m demo.agent_demo
```

Or with the helper that does steps 4+5:

```bash
./demo/run_scenario.sh
```

### Docker

```bash
docker compose up --build
```

Verified end-to-end with OrbStack on macOS — see `docker-compose.yml`.

### Use as a Claude Code firewall

`tools/aegis_hook.py` is a `PreToolUse` hook that fires before every
tool call inside Claude Code, asks the running Aegis service for a
verdict, and short-circuits the tool with stderr if blocked.

```bash
# 1. Service running
docker compose up -d

# 2. Add to ~/.claude/settings.json:
#    {
#      "hooks": { "PreToolUse": [{
#        "matcher": "*",
#        "hooks": [{
#          "type": "command",
#          "command": "python3 /ABS/PATH/MVP/tools/aegis_hook.py"
#        }]
#      }] }
#    }

# 3. Smoke test (10 cases: ALLOW, BLOCK, APPROVAL, FAIL_OPEN, ...)
bash tools/test_hook.sh
```

Full install + env vars + tool mapping table: see [`tools/README.md`](tools/README.md).

---

## Configuration

Copy `.env.example` to `.env` and fill in API keys when ready.

The defaults are deliberately offline-friendly:

| Setting | Default | Switch to real backend |
|---|---|---|
| `AEGIS_EMBEDDING_PROVIDER` | `dummy` | `openai` (needs `OPENAI_API_KEY`) |
| `AEGIS_JUDGE_PROVIDER` | `dummy` | `haiku` (needs `ANTHROPIC_API_KEY`) |

If you set the provider to `openai` / `haiku` but the corresponding key
is missing, the code automatically falls back to the dummy implementation
so nothing breaks.

---

## Demo output (PLAN 1.3 Definition of Done)

```
=== AegisData T2 demo (mode=stub, aid=agent-demo-XXXX) ===
Tool-call verdicts:
  1. read_file        → ALLOW              (all firewall steps passed)
  2. write_file       → ALLOW              (all firewall steps passed)
  3. execute_shell    → BLOCK              (dangerous pattern: \brm\s+-rf\s+/)
  4. write_file       → REQUIRE_APPROVAL   (exp_bytes_write 5000000000 > budget 1000000000)
  5. transfer_funds   → REQUIRE_APPROVAL   (blast radius 10 >= 7)

Verdict tally:
  ALLOW             2
  BLOCK             1
  REQUIRE_APPROVAL  2

Audit chain for agent-demo-XXXX:
  length      = 5
  head        = 4a2866d5c92545fec8d4e282...
  chain_valid = True
```

* **Stub mode** (no `ANTHROPIC_API_KEY`): exercises Aegis with a fixed 5-call scenario hand-tuned to hit every verdict class.
* **Live mode** (`ANTHROPIC_API_KEY` set): asks Claude Sonnet 4.6 with the tool catalog and forwards each `tool_use` block to Aegis.

---

## API reference

### `POST /evaluate`

> **Breaking change in M8 (patent v7.10 alignment):** the `cost_estimate`
> field switched from the legacy `{exp_bytes_write, exp_dollars,
> confidence}` trio to the patent's 16-named-slot
> `CostEfficiencyMetrics` shape (`input_token_count`,
> `output_token_count`, `cumulative_dollars`,
> `forecasted_cost_to_completion`, `budget_burn_rate`, `cache_hit_rate`,
> `task_progress_score`, …). All fields default to 0; populate what you
> can measure on the host.

Request body (`ATVInput`):

```json
{
  "header": {
    "trace_id": "t-001",
    "span_id":  "s-001",
    "tenant_id": "demo-tenant",
    "aid": "agent-42",
    "ats": "ATV-2080-v1",
    "schema_version": "ATV-2080-v1",
    "tier_profile": "T2",
    "cost_attestation_profile": "software",
    "timestamp_ns": 1737172800000000000
  },
  "agent_state_text": "User asked for a file summary...",
  "plan_text": "Read the file, then write summary.",
  "tool_name": "read_file",
  "tool_args_json": "{\"path\":\"./data/report.txt\"}",
  "safety_flags": {"prompt_injection": 0.02},
  "memory_fingerprint": "sha3_256:abcdef01234567...",
  "cost_estimate": {
    "input_token_count": 200,
    "output_token_count": 80,
    "cumulative_dollars": 0.001,
    "forecasted_cost_to_completion": 0.01,
    "budget_burn_rate": 0.05
  }
}
```

Response (`Verdict`):

```json
{
  "decision": "ALLOW",
  "reason": "all firewall steps passed",
  "atv_id": "6f8b7c5a-...",
  "signature": "7a2c...",
  "confidence": 1.0,
  "step_traces": {
    "aegis.firewall.step310_args.run":     "step310: ok (inj=0.02)",
    "aegis.firewall.step320_blast.run":    "step320: blast=1 (tool=read_file)",
    "aegis.firewall.step330_human.run":    "step330: ok (blast=1)",
    "aegis.firewall.step335_cost.run":     "step335: ok (cum=0.001, forecast=0.01, ceiling=1.0)",
    "aegis.firewall.step340_policy.run":   "step340: allow match safe-read",
    "aegis.firewall.step370_exec.annotate":"step370: exec-recommendation=PROCEED",
    "aegis.atmu.intent_log":               "intent_record_id=e4a09dae-7c44-...",
    "aegis.burnin.composite_score":        "composite=0.000"
  }
}
```

The host SHOULD use the `intent_record_id` value from `step_traces` to
post a follow-up `/tool-outcome` after running (or skipping) the tool.

### `POST /approve`

Append a signed human-decision record onto an aid's chain (typically
following a previous `REQUIRE_APPROVAL`):

```json
{
  "atv_id": "6f8b7c5a-...",
  "aid": "agent-42",
  "tenant_id": "demo-tenant",
  "approver": "alice",
  "decision": "ALLOW",
  "note": "manually reviewed"
}
```

### `POST /tool-outcome` (M10)

After executing or skipping a tool, the host informs Aegis of the
outcome so the ATMU intent record transitions out of `prepared` /
`committed`:

```json
{
  "record_id": "e4a09dae-7c44-...",
  "status": "success",
  "result_hash": "sha3-256-of-tool-output",
  "side_effect_receipt": "txn-confirmation-id",
  "follow_up_state": "committed",
  "follow_up_reason": "human approved"
}
```

Status ∈ `{success, failure, timeout, partial, compensated}`.
`follow_up_state` ∈ `{prepared, committed, aborted, rolled-back, compensated, quarantined}`
and is validated by the state machine — illegal transitions return 409.

### `GET /audit/{aid}`

Returns the full Merkle-chained signed record list for one agent, plus a
`chain_valid` flag (server re-runs `verify_chain` so callers don't have
to). Each record carries the M11 `cost_attestation_hint` boolean so a
future Cost Attestation Ledger can index cost-influenced records.

### `GET /burnin-status` (M11)

Returns the per-layer slot table for the 5-layer Burn-in controller:

```json
{
  "layers": [
    {"key":"L1","layer":"L1","tenant_id":null,"role_id":null,"aid":null,
     "phase":"observation","samples":3,"tpr":0.0,"fpr":0.0,
     "precision":0.0,"override_rate":0.0,"transitions":[]},
    {"key":"L2:demo-tenant","layer":"L2","tenant_id":"demo-tenant",...},
    ...
  ],
  "expected_samples":{"L1":1000,"L2":5000,"L3":2000,"L4":1000,"L5":500},
  "weights":{"L1":0.20,"L2":0.20,"L3":0.20,"L4":0.20,"L5":0.20}
}
```

`POST /burnin/graduate {layer_key}` advances one slot if its phase gates
are met (409 with reason otherwise). `POST /burnin/label {inp, verdict,
ground_truth, was_human_override?}` feeds labelled outcomes into
TPR/FPR/precision so Shadow → Assisted graduation can be evaluated.

---

## Tests

```bash
uv run pytest --cov=aegis
```

* **243 tests** (M8: +12 ATV builder · M9: +9 firewall split · M10: +37 ATMU · M11: +21 burn-in, on top of the original 164)
* **Strict mypy** over 46 source files
* **Concurrency**: 100-record SQLite audit chain, 200-line JSONL, 100-intent ATMU WAL all pass under thread contention
* **No network in tests**: respx mocks `api.anthropic.com`; OpenAI is unused under `dummy` provider

---

## Where to look

```
src/aegis/
├── schema.py              ATV slice constants + Pydantic models
├── config.py              pydantic-settings (.env loader)
├── main.py                FastAPI factory + `app`
├── atv/
│   ├── embeddings.py      EmbeddingProvider abstraction
│   └── builder.py         build_atv() — fills the 2080-D vector
├── firewall/
│   ├── core.py            FirewallContext + run_firewall orchestrator
│   ├── step310_args.py    pattern blocklist + injection threshold
│   ├── step320_blast.py   tool blast-radius lookup
│   ├── step330_human.py   high-blast → REQUIRE_APPROVAL
│   ├── step335_cost.py    per-tenant byte/dollar/confidence budgets
│   └── step340_policy.py  policy match + sLLM judge fallback
├── judge/
│   ├── base.py            Judge ABC + JudgeVerdict
│   ├── haiku.py           Claude Haiku 4.5 backend
│   └── dummy.py           offline stub
├── sign/
│   ├── ed25519.py         keypair management + sign/verify
│   └── merkle.py          chain hashing + verify_chain
├── audit/
│   ├── sqlite_store.py    indexed records + chain head (transactional)
│   └── jsonl_store.py     append-only raw record dump
└── api/
    ├── evaluate.py        POST /evaluate
    ├── approve.py         POST /approve
    └── audit_query.py     GET  /audit/{aid}

policies/default.json      deny + allow rules (PLAN 6.9)
demo/agent_demo.py         5-call scenario hitting every verdict class
demo/run_scenario.sh       bring service up + run demo
```

---

## Out of scope (per PLAN 1.2)

Real TEE deployment, hardware EK burn-in (L1), post-quantum signatures,
CSD integration, in-storage similarity, web UI. These are T3 / future
work.
