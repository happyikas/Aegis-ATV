# AegisData T2 MVP

A Python sidecar that wraps every AI-agent tool call in a 2,080-dimensional
**Agent Trace Vector (ATV)**, runs it through a 5-step Action Firewall,
asks an sLLM judge if the static rules don't decide it, then **Ed25519-signs**
the result and chains it into a **Merkle-linked SQLite + JSONL audit log**.

Implementation of the T2 tier described in `PLAN.md`.

---

## What's in the box

| Layer | Module | Notes |
|---|---|---|
| Schema | `aegis.schema` | `ATV-2080-v1` slice constants, Pydantic v2 models |
| Vector | `aegis.atv.builder` + `embeddings` | 2080-D ATV; OpenAI / dummy / BGE backends |
| Firewall | `aegis.firewall.{core, step310..step340}` | 5 steps + sLLM fallback |
| Judge | `aegis.judge.{base, haiku, dummy}` | Claude Haiku 4.5; dummy fallback when no key |
| Sign | `aegis.sign.{ed25519, merkle}` | PEM PKCS8, canonical-JSON Merkle chain |
| Audit | `aegis.audit.{sqlite_store, jsonl_store}` | WAL + lock + `BEGIN IMMEDIATE` |
| API | `aegis.main`, `aegis.api.*` | `/healthz`, `/evaluate`, `/approve`, `/audit/{aid}` |

Hardware band (200-D, indices 1880..2080) is intentionally zero-filled — that's the T3 work.

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

(Dockerfile + `docker-compose.yml` are provided. Build/run validation
is deferred until Docker Desktop is installed locally.)

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

Request body (`ATVInput`):

```json
{
  "header": {
    "trace_id": "t-001",
    "span_id":  "s-001",
    "tenant_id": "demo-tenant",
    "aid": "agent-42",
    "ats": "ATV-2080-v1",
    "timestamp_ns": 1737172800000000000
  },
  "agent_state_text": "User asked for a file summary...",
  "plan_text": "Read the file, then write summary.",
  "tool_name": "read_file",
  "tool_args_json": "{\"path\":\"./data/report.txt\"}",
  "safety_flags": {"prompt_injection": 0.02},
  "memory_fingerprint": "sha3_256:abcdef01234567...",
  "cost_estimate": {
    "exp_bytes_write": 1024,
    "exp_dollars": 0.0001,
    "confidence": 0.9
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
    "aegis.firewall.step310_args.run":   "step310: ok (inj=0.02)",
    "aegis.firewall.step320_blast.run":  "step320: blast=1 (tool=read_file)",
    "aegis.firewall.step330_human.run":  "step330: ok (blast=1)",
    "aegis.firewall.step335_cost.run":   "step335: ok (...)",
    "aegis.firewall.step340_policy.run": "step340: allow match safe-read"
  }
}
```

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

### `GET /audit/{aid}`

Returns the full Merkle-chained signed record list for one agent, plus a
`chain_valid` flag (server re-runs `verify_chain` so callers don't have to).

---

## Tests

```bash
uv run pytest --cov=aegis
```

* **101 tests** (90 unit + 11 integration)
* **Coverage 95%** (PLAN DoD: ≥70%)
* No network: respx mocks `api.anthropic.com`; OpenAI is unused under `dummy` provider
* Concurrency tests cover 100-record SQLite chain and 200-line JSONL appends

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
