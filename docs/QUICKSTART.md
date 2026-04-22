# Quickstart

**Goal**: get the firewall running, send one tool call, see the
verdict + the signed audit chain — all in 60 seconds.

This guide assumes you have **either** Docker (recommended) **or**
[`uv`](https://github.com/astral-sh/uv) installed. Pick one path.

---

## Path A — Docker (recommended)

```bash
git clone <this repo>
cd MVP
docker compose up -d --build      # ~30s on a warm cache
```

Wait for the health check:

```bash
until curl -sf localhost:8000/healthz >/dev/null; do sleep 1; done
echo "ready"
```

Send your first tool call:

```bash
curl -s localhost:8000/evaluate -H 'content-type: application/json' -d '{
  "header": {
    "trace_id": "demo-001",
    "span_id":  "span-001",
    "tenant_id": "demo-tenant",
    "aid": "quickstart-agent",
    "ats": "ATV-2080-v1",
    "schema_version": "ATV-2080-v1",
    "tier_profile": "T2",
    "cost_attestation_profile": "software",
    "timestamp_ns": 1737172800000000000
  },
  "agent_state_text": "user asked to read a file",
  "plan_text": "read ./data/report.txt",
  "tool_name": "read_file",
  "tool_args_json": "{\"path\":\"./data/report.txt\"}",
  "safety_flags": {"prompt_injection": 0.02},
  "cost_estimate": {
    "input_token_count": 100,
    "cumulative_dollars": 0.0001,
    "forecasted_cost_to_completion": 0.01
  }
}' | jq '{decision, reason, atv_id, signature: .signature[:20]}'
```

Expected output:

```json
{
  "decision": "ALLOW",
  "reason": "all firewall steps passed",
  "atv_id": "…",
  "signature": "…"
}
```

Inspect the audit chain for that AID:

```bash
curl -s localhost:8000/audit/quickstart-agent | jq '{length, head: .head[:16], chain_valid}'
```

Open the dashboard:

```bash
open http://localhost:8000        # macOS
xdg-open http://localhost:8000    # Linux
```

Tear down:

```bash
docker compose down
```

---

## Path B — uv (local Python, no Docker)

```bash
git clone <this repo>
cd MVP

# Install deps (uv will fetch Python 3.11+ if missing)
uv sync

# Boot the service
uv run uvicorn aegis.main:app --reload --port 8000
```

In a second shell, run the same `curl` from Path A. Or run the full
demo (all M8–M16 scenarios):

```bash
uv run python -m demo.agent_demo
```

---

## Verifying the install

If something feels off, these three commands rule out 90% of issues:

```bash
uv run pytest -q                        # → 326 passed
uv run ruff check .                     # → All checks passed!
uv run mypy src                         # → Success: no issues found in 61 source files
```

---

## What just happened

The `/evaluate` POST went through this pipeline:

```
ATVInput JSON
   ↓
build 2080-D Agent Trace Vector  (aegis.atv.builder)
   ↓
step 310 — argument inspection (regex deny-list, injection threshold)
step 315 — AID-region authorization (per-AID quarantine check)
step 320 — blast radius lookup
step 330 — high-blast → REQUIRE_APPROVAL
step 335 — forecasted cost gating (16-slot CostEfficiencyMetrics)
step 340 — policy match + sLLM judge fallback (with attribution head)
step 350 — approval dispatch (if REQUIRE_APPROVAL)
step 360 — Ed25519 sign + Merkle-chain append + AES-GCM journal write
step 370 — exec recommendation annotation (PROCEED/SUPPRESS/DEFER)
   ↓
ATMU intent log: tentative → prepared
   ↓
Burn-in observation: bump per-layer sample counters
   ↓
Verdict response (with step_traces showing each step's decision)
```

Every record gets:

* An **Ed25519 signature** over the canonical-JSON record
* A **SHA3-256 Merkle link** to the previous record for that AID
* An **AES-256-GCM journal entry** with AAD bound to identity tuple
* (If `cost_attestation_hint`) a **Cost Attestation Record** signed
  with a separate Ed25519 key

---

## Next steps

* [`docs/DEMO.md`](DEMO.md) — recording playbook for screencasts
* [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — per-milestone surface
  tour with file pointers
* [`docs/OPERATIONS.md`](OPERATIONS.md) — production runbook (env
  vars, key rotation, AID admin, journal forensics, backup/restore)
* [`tools/README.md`](../tools/README.md) — wire Aegis into Claude
  Code as a `PreToolUse` hook
