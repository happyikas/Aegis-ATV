# Demo Recording Playbook

Two scripts, both recordable in one take with no editing tricks:

* **§1 — 90-second elevator demo** for a recruiter / investor / hiring
  manager who has 90 seconds and wants to see "this thing actually
  works." One terminal, one browser tab, no narration cuts.
* **§2 — 5-minute deep dive** for a technical reviewer who wants to see
  every M8–M16 surface in motion. Two terminals, dashboard + curl
  side-by-side.

Both assume:

* You're on macOS or Linux with **Docker** or `uv` installed.
* The repo is checked out to a writable path (`./data/` and `./keys/`
  get auto-created on first boot).
* Your browser is at zoom 100% and a 1280×800 window — the dashboard
  is laid out for that.

---

## Pre-flight (do this once before recording)

```bash
# 1. Pristine state — discard previous demo data
rm -rf data/ keys/

# 2. Verify the test suite still passes (so you don't film a regression)
uv run pytest -q
# expected: 326 passed in ~2s

# 3. Pre-pull the image so the recording doesn't start with a 30s build
docker compose build
```

Recommended recorder: **`asciinema`** for terminal scenes, **OBS** /
**QuickTime** for the browser. If you only have one tool, use OBS for
both — record the whole desktop and zoom in post.

---

## §1 — 90-second elevator demo

**Goal**: prove the firewall is real, signed, chained, and visible.
**Outcome the viewer should leave with**: "every tool call goes through
this, every record is signed, and the chain is live."

### Beats

| Time | Scene | Action | What to say |
|---|---|---|---|
| 0:00–0:05 | Title card | (static) **AegisData T2 — Action Firewall for AI Agents** | — |
| 0:05–0:15 | Terminal | `docker compose up -d && curl -s localhost:8000/healthz \| jq` | "One container. Service is up. Notice the `burn_in_id` — that's a signed measurement of the running code, computed at startup." |
| 0:15–0:30 | Browser → `localhost:8000` | Page loads. Point at the green "service healthy" dot. | "Single-page dashboard. Every panel here talks to a real endpoint — there's no mock data." |
| 0:30–0:50 | Browser | Click **"Run demo"** in the upper-left. Watch the pipeline animate. | "We're crafting five tool calls — read, write, `rm -rf /`, a 5GB write, and a half-million-dollar transfer. Watch the pipeline: arg inspection, AID auth, blast radius, cost forecast, policy + sLLM, approval, audit." |
| 0:50–1:05 | Browser | Scroll to the **Audit chain** panel. Point at the green ✓ valid badge. | "Five records. Each one signed with Ed25519, each one Merkle-chained — `prev_hash → this_hash`. The chain validates client-side." |
| 1:05–1:20 | Browser | Scroll to the **Forensic replay** panel. Click **Replay**. | "Every audit record is also written to an AES-256-GCM encrypted journal. This decrypts the whole journal end-to-end and reconstructs the per-AID hash chain. Tampered records would surface here." |
| 1:20–1:30 | Closing card | (static) **326 tests · ruff clean · mypy strict · 16 milestones** | "Sixteen patent-aligned milestones, fully tested, runnable in a single container." |

### Recording command (terminal portion)

```bash
asciinema rec demo-90s.cast \
  --title "AegisData T2 — 90-second demo" \
  --idle-time-limit 1.5
```

Then in the recording shell run, in this order:

```bash
docker compose up -d
sleep 2
curl -s localhost:8000/healthz | jq
open http://localhost:8000   # macOS; use xdg-open on Linux
# (now drive the browser as in the beats table)
```

### Tearing down

```bash
docker compose down
```

---

## §2 — 5-minute deep-dive

**Goal**: walk every M8–M16 surface with the right amount of context for
a technical reviewer to understand what each panel proves.
**Outcome**: "I see the full attack surface and the full audit surface."

Layout: split the screen vertically. Left half = browser at
`localhost:8000`. Right half = terminal at the repo root.

### Beats

| Time | Side | Action | What to say |
|---|---|---|---|
| **0:00 — Setup** |  |  |  |
| 0:00–0:15 | Term | `docker compose up -d && curl -s localhost:8000/healthz \| jq` | "Single container, ed25519 signing key + cost-attestation signing key + journal data-key all auto-generated on first boot." |
| 0:15–0:25 | Browser | Refresh dashboard. Point at version + burn_in_id. | "Burn-in id is a signed hash of the running code, policies, embedding/judge providers, and public key. The dashboard verifies that signature in the browser using WebCrypto." |
| **0:25 — M8/M9: ATV + Firewall** |  |  |  |
| 0:25–0:55 | Browser | Click preset **"safe read"** → click **Evaluate →** | "We're sending an ATV-2080-v1 — 2080 float32 dimensions, 30 named subfields per the patent's Appendix A. Look at the right-side **band strip** — header, agent_state, plan, tool_call, safety_flags, memory_fp, cost_eff. The hardware band stays gray because we're in T2." |
| 0:55–1:15 | Browser | Click preset **"DROP TABLE"** → **Evaluate →** | "Same pipeline, different inputs. Arg-inspection step blocks SQL DROP statically — no LLM call needed. Cheap fast path. The reason string and the affected step both surface in the trace." |
| 1:15–1:35 | Browser | Click preset **"transfer $500"** → **Evaluate →** | "Now the cost forecast and blast radius force REQUIRE_APPROVAL. Look at step 350 — the approval dispatch fires; step 360 still signs the record; step 370 annotates exec recommendation as DEFER." |
| **1:35 — M11: Burn-in** |  |  |  |
| 1:35–2:00 | Browser | Scroll to **Burn-in baseline**. Point at the L1–L5 rows. | "Five layer slots — hardware, tenant, topology, role, instance. Each progresses observation → shadow → assisted → production. Gates are 1000 samples, then TPR≥0.95 / FPR≤0.02 / precision≥0.90, then override-rate ≤5%. Right now they're in observation because we've only sent a few calls." |
| **2:00 — M14: AID circuit breaker** |  |  |  |
| 2:00–2:20 | Term | `curl -s localhost:8000/admin/aid \| jq` | "Per-AID authorization table. We define roles — read-only-role, financial-role — in `policies/aid_region.json`. Three violations and the AID gets quarantined automatically." |
| 2:20–2:50 | Term | `AEGIS_DEMO_SKIP_EXTRAS= AEGIS_URL=http://localhost:8000 uv run python -m demo.agent_demo` | (let the M14 scenario play; point at "violation 1/3 → 2/3 → 3/3 → quarantine → admin release") "Software emulation of patent §5B. T3 puts the same logic in the hardware tag comparator on the CSD — we keep the schema and external contract identical so the swap is mechanical." |
| 2:50–3:00 | Browser | Refresh **AID circuit breaker** panel. | "Live in the dashboard. The 'use ↑' button autofills the release form; the admin token gate is the T2 stand-in for the patent's signed administrative recovery policy." |
| **3:00 — M12: Cost Attestation Ledger** |  |  |  |
| 3:00–3:25 | Term | `curl -s localhost:8000/cost-attestation/by-tenant/demo-tenant \| jq '.records[0]'` | "Separate Ed25519 signing key from the telemetry key — Claim 34. Three divergence metrics: token-to-FLOPs, memory-cost, dollar-cost. Each Cost Attestation Record binds a SHA3 commitment of the ATV; the ledger has its own per-AID Merkle chain." |
| **3:25 — M16: HAM** |  |  |  |
| 3:25–4:00 | Browser | Scroll to **Hierarchical Agent Memory**. Type in the body field, click **memory →** twice with different tags. Then **recall** with a tag filter, then **context**. | "Patent ¶[0102C] — 4-level memory hierarchy. T2 emulates L3+L4 with an encrypted SQLite store + an in-process LRU. Every body is AES-256-GCM with AAD bound to the (tenant_id, aid, seq) tuple — decryption rejects if any of those is tampered." |
| 4:00–4:15 | Browser | Check two HAM items, type a claim, click **bind** in the ground panel. | "Ground binds a claim to N memory references. The SHA3 claim hash + the resolved subset is what your downstream agent quotes. Missing references surface as a separate list — no silent dropping." |
| **4:15 — M15: Forensic replay** |  |  |  |
| 4:15–4:45 | Browser | Click **Replay** in the forensic-replay panel. | "Walks the encrypted ATV journal end-to-end. The cleartext header — schema_version, key_version, tenant_id, aid, atv_commitment, ts_ns — is used as AES-GCM additional-authenticated-data. So if anyone flipped a bit in either the header or the ciphertext, we get an auth-tag failure here, not at audit-display time." |
| 4:45–5:00 | Term | `curl -s localhost:8000/forensic/replay \| jq '{decrypted_count, tampered_count, per_aid_chain_valid}'` | "Per-AID chain reconstruction. All chains valid means every AID's record sequence is intact. Tampered count > 0 would name the offending records." |

### Recording command

```bash
# Two-pane layout: tmux is convenient
tmux new-session -s demo -d -x 200 -y 50
tmux send-keys -t demo:0 "clear" Enter

# Then either record the whole desktop with OBS,
# or split asciinema for the terminal pane:
asciinema rec demo-5min.cast \
  --title "AegisData T2 — 5-minute deep dive" \
  --idle-time-limit 2.0 \
  --command "tmux attach -t demo"
```

### Tearing down

```bash
docker compose down
rm -rf data/ keys/   # reset for the next take
```

---

## §3 — Auto-runnable end-to-end script

If you'd rather have a hands-off recording — e.g. a CI artifact or a
GIF generator — this single script reproduces everything the
deep-dive demonstrates, in order, without a browser:

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "=== boot ==="
docker compose up -d --build
until curl -sf http://localhost:8000/healthz >/dev/null; do sleep 1; done
echo "service up — $(curl -s localhost:8000/healthz | jq -c)"

echo
echo "=== M8/M9 — five-call firewall scenario ==="
AEGIS_DEMO_SKIP_EXTRAS=1 AEGIS_URL=http://localhost:8000 \
  uv run python -m demo.agent_demo

echo
echo "=== M14 — AID circuit breaker ==="
echo "(see demo.agent_demo run_circuit_breaker_scenario)"

echo
echo "=== M16 — HAM ==="
echo "(see demo.agent_demo run_ham_scenario)"

echo
echo "=== full demo (all scenarios) ==="
AEGIS_URL=http://localhost:8000 uv run python -m demo.agent_demo

echo
echo "=== M15 — forensic replay ==="
curl -s localhost:8000/forensic/replay | jq '{decrypted_count, tampered_count, aids_seen, per_aid_chain_valid}'

echo
echo "=== M12 — cost attestation ==="
curl -s localhost:8000/cost-attestation/by-tenant/demo-tenant | jq '.length, (.records[:1])'

echo
echo "=== teardown ==="
docker compose down
```

Save as `demo/record.sh`, run with `bash demo/record.sh`. Total
runtime: ~25 seconds on a warm Mac mini. Pipe through `tee` and you
have a transcript ready to attach to a PR.

---

## §4 — What the camera should NOT see

* **`.env`** — even if it's empty in dev, never let it on screen. The
  recording shouldn't begin until you've run `cat .env` in private
  to check it's not the production one.
* **`./keys/*.pem`** — same. The signing keys are dev-only fixtures
  but a viewer can't tell.
* **Terminal scrollback** with `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`
  in `env`. Run `clear; printenv | grep -v API_KEY` before opening the
  recording shell.
* **`docker logs aegis-atv`** unless you've grepped out request
  bodies — they may contain user prompts.

---

## §5 — Stills for slides / blog post

Single-frame screenshots that work well in a deck:

| Frame | Caption suggestion |
|---|---|
| Dashboard "Action Firewall pipeline" mid-evaluate, three steps green, two pending | "Per-call trace through every firewall stage" |
| "Audit chain" panel showing 5 records with `prev_hash → this_hash` columns | "Ed25519-signed, Merkle-chained" |
| "Burn-in attestation" panel with the ✓ verified (browser Ed25519) badge lit | "Code attestation verified client-side" |
| "Forensic replay" tiles showing 10 decrypted / 0 tampered / 2/2 chains valid | "AES-256-GCM journal, end-to-end replayable" |
| HAM viewer with three items listed and a ground claim_hash visible | "Hierarchical Agent Memory with provenance binding" |
| `/theater` page with the band strip annotated | "ATV-2080-v1 — 30 subfields, hardware band reserved" |

`Cmd-Shift-4 + Space` on macOS captures a single window cleanly.

---

## §6 — Common gotchas during recording

| Symptom | Cause | Fix |
|---|---|---|
| `service unreachable` red dot in dashboard header | Container still booting | Wait 2s and refresh, or curl `/healthz` first |
| Five-call demo runs but no AID quarantine appears | `AEGIS_DEMO_SKIP_EXTRAS=1` is set | `unset AEGIS_DEMO_SKIP_EXTRAS` |
| Forensic replay shows 0 decrypted | `/evaluate` was never called | Run `Run demo` once, then click Replay |
| Browser Ed25519 verify badge says "unsupported" | Old Safari / Firefox without `crypto.subtle` Ed25519 | Use Chrome ≥ 113 or Safari ≥ 17 |
| HAM "object not found for aid" on ground | You stored items under a different aid in another tab | Make sure the `ham-aid` field matches the aid you stored under |
| `/admin/aid/release` returns 401 | Admin token mismatch | Check `AEGIS_ADMIN_TOKEN` env (default `dev-admin-token`) |
