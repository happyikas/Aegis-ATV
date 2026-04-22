# Operations Runbook

Production deployment guide for the AegisData T2 sidecar. Covers env
vars, key management, AID admin, journal forensics, and backup/restore.
For the Mac mini single-user case, see [`SETUP_MACMINI.md`](../SETUP_MACMINI.md)
instead — this document assumes a server deployment.

---

## 1. Environment variables

All variables have safe defaults. Override only what you need to change.

### Provider switches

| Variable | Default | Production setting |
|---|---|---|
| `AEGIS_EMBEDDING_PROVIDER` | `dummy` | `openai` (needs `OPENAI_API_KEY`) |
| `AEGIS_JUDGE_PROVIDER` | `dummy` | `haiku` (needs `ANTHROPIC_API_KEY`) |
| `AEGIS_SAFETY_PROVIDER` | `dummy` | `openai` (Moderations API) or `haiku` |

If a provider is set to `openai` / `haiku` but the corresponding API
key is missing or unreachable, the code automatically falls back to
the dummy implementation rather than failing the request.

### Storage paths

| Variable | Default | Notes |
|---|---|---|
| `AEGIS_AUDIT_DB` | `./data/audit.sqlite` | WAL mode, indexed by aid |
| `AEGIS_AUDIT_JSONL` | `./data/audit.jsonl` | Append-only mirror |
| `AEGIS_INTENT_LOG_DB` | `./data/intent_log.sqlite` | ATMU WAL |
| `AEGIS_COST_LEDGER_DB` | `./data/cost_attestation.sqlite` | Separate ledger |
| `AEGIS_COST_LEDGER_JSONL` | `./data/cost_attestation.jsonl` | Mirror |
| `AEGIS_JOURNAL_PATH` | `./data/journal.bin` | AES-256-GCM journal |
| `AEGIS_HAM_DB` | `./data/ham.sqlite` | Hierarchical Agent Memory |
| `AEGIS_POLICY_DIR` | `./policies/` | JSON policy files |

### Key paths

| Variable | Default | Purpose |
|---|---|---|
| `AEGIS_SIGNING_KEY_PATH` | `./keys/ed25519.pem` | Telemetry Ed25519 private |
| `AEGIS_PUBLIC_KEY_PATH` | `./keys/ed25519.pub` | Telemetry public |
| `AEGIS_COST_SIGNING_KEY_PATH` | `./keys/ed25519_cost.pem` | Cost ledger private (Claim 34: distinct key) |
| `AEGIS_COST_PUBLIC_KEY_PATH` | `./keys/ed25519_cost.pub` | Cost ledger public |
| `AEGIS_JOURNAL_DATA_KEY_PATH` | `./keys/journal_data.key` | AES-256 raw (32 bytes) |
| `AEGIS_HAM_DATA_KEY_PATH` | `./keys/ham_data.key` | AES-256 raw (32 bytes) |

### Admin

| Variable | Default | Purpose |
|---|---|---|
| `AEGIS_ADMIN_TOKEN` | `dev-admin-token` | Bearer for `POST /admin/aid/release`. **Change this in production.** |

---

## 2. Key management

All four keys are auto-created on first boot if missing. For
production:

1. **Pre-generate and mount, don't auto-create.** Auto-creation makes
   onboarding easy but means the keys are written to whatever volume
   the container has — possibly a transient one.
2. **Use a secrets manager** (Vault, AWS Secrets Manager, GCP Secret
   Manager) and mount the keys into `./keys/` at container start.
3. **Separate the cost-ledger key from the telemetry key** — that's
   the whole point of Claim 34. They MUST live in different secret
   stores or different rotation schedules.
4. **The two AES-256 raw keys** (`journal_data.key`, `ham_data.key`)
   are 32 bytes of unstructured randomness. Generate with:
   ```bash
   head -c 32 /dev/urandom > journal_data.key
   chmod 400 journal_data.key
   ```

### Key rotation

The two Ed25519 telemetry/cost keys can be rotated by:

1. Generate a new keypair.
2. Replace `keys/ed25519.pem` (or `_cost.pem`) and the `.pub` sibling.
3. Restart the service.

**Existing chain segments stay verifiable** because each record
embeds the public key fingerprint that signed it. The next record
appended after rotation seals the cutover — so the chain has a
visible "key rotation" boundary you can detect at audit time.

The two AES-256 data keys (journal, HAM) are **not** rotatable
in-place — rotating them would invalidate every existing encrypted
record. If you need to rotate, the patent's pattern is:

1. Stand up a new ledger with the new key.
2. Decrypt every old record with the old key, re-encrypt with the new.
3. Verify the per-AID chain reconstructs identically.
4. Cut over and decommission the old key.

The `aegis.audit.replay.replay()` function provides the read side of
that flow today; the write side is left to ops scripts because the
right answer depends on your specific durability requirements.

---

## 3. AID admin (M14)

### Listing quarantined AIDs

```bash
curl -s localhost:8000/admin/aid | jq
```

Shape:

```json
{
  "quarantined": [
    {
      "aid": "agent-runaway-42",
      "violations": 5,
      "quarantined_at_ns": 1737172800123456789,
      "reason": "violations 5 ≥ max 5: unauthorized_tool:execute_shell"
    }
  ]
}
```

### Inspecting one AID

```bash
curl -s localhost:8000/admin/aid/agent-runaway-42 | jq
```

Returns the full violation history for that AID, including
non-quarantine events.

### Releasing an AID

```bash
curl -s localhost:8000/admin/aid/release \
  -H "X-Aegis-Admin-Token: $AEGIS_ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"aid": "agent-runaway-42", "reason": "human reviewed; cause: misconfigured tool whitelist"}' | jq
```

Returns `{ok: true, aid, status: "normal", violations: 0}` on success.
Status returns to `normal` and the violation counter is reset.

### Configuring per-AID policies

Edit `policies/aid_region.json`:

```json
{
  "default_policy": {
    "allowed_tools": [],          // empty = no restriction
    "allowed_paths": [],
    "max_violations": 5
  },
  "aids": {
    "tenant-A:financial-role": {
      "allowed_tools": ["read_file", "transfer_funds"],
      "allowed_paths": ["./data/finance/"],
      "max_violations": 1
    },
    "tenant-A:read-only-role": {
      "allowed_tools": ["read_file", "list_directory"],
      "allowed_paths": ["./data/"],
      "max_violations": 3
    }
  }
}
```

Keys are `"{tenant_id}:{role_id}"`. The role comes from
`ATVInput.role_id`; if absent, the policy lookup uses the literal
string `"default-role"`. Empty `allowed_tools` means **no
whitelist** (allow anything); empty `allowed_paths` means no path
constraint. Changes are picked up on the next service restart (the
policy is `lru_cache`d).

---

## 4. Journal forensics (M15)

### Routine health check

```bash
curl -s localhost:8000/forensic/replay | jq '{
  available,
  decrypted_count,
  tampered_count,
  per_aid_chain_valid
}'
```

If `tampered_count > 0` or any value in `per_aid_chain_valid` is
`false`, you have a problem.

### Reading individual records

The replay endpoint already includes a `tampered_records` array
listing the bad records. For deeper inspection:

```python
from pathlib import Path
from aegis.audit.encrypted_journal import EncryptedJournal, load_or_create_data_key

key = load_or_create_data_key(Path("./keys/journal_data.key"))
j = EncryptedJournal(path=Path("./data/journal.bin"), data_key=key)

for record in j.iter():  # yields decrypted dicts in order
    print(record["header"]["aid"], record["header"]["ts_ns"])
```

### What to do if tampering is detected

1. **Don't restart.** A restart writes to the journal and may obscure
   the tampering pattern.
2. **Snapshot `data/journal.bin`** to an offline location.
3. **Compare against the audit SQLite** — they're written in the
   same step-360 transaction so they MUST agree. If they don't,
   you've narrowed the attack window.
4. **Check the public key fingerprints** in surrounding records —
   if a key rotation happened, the tampering may have happened at
   the rotation boundary.
5. **Pull the cost-attestation ledger** for the same time window —
   if the cost key was tampered with, the divergence metrics will
   diverge from the SW-band predictions.

---

## 5. Cost attestation (M12)

### Per-AID query

```bash
curl -s localhost:8000/cost-attestation/agent-42 | jq '{
  aid,
  length,
  head: .head[:16],
  chain_valid,
  total_dollars: ([.records[].cumulative_dollars] | add)
}'
```

### Per-tenant aggregation

```bash
curl -s localhost:8000/cost-attestation/by-tenant/tenant-A | jq '{
  tenant_id,
  length,
  head: .head[:16],
  chain_valid
}'
```

### When divergence triggers escalation

Per Claim 27, divergence escalation runs **independently of the sLLM
verdict**. If `aegis.cost.escalation.evaluate_escalation()` returns
`should_escalate: true`, the firewall flags the next call from that
AID — even if the call itself is clean.

The 3× baseline threshold is hard-coded in
`src/aegis/cost/escalation.py`. To tune for your workload, override
it via a custom escalation policy (PR welcome — see PLAN_v2 §3.3).

---

## 6. Backup & restore

### What to back up

```
data/   ← all SQLite databases + journal.bin + jsonl mirrors
keys/   ← all 4 key files
policies/   ← JSON policy files (already in git, but back up edits)
```

### Snapshot procedure

SQLite is in WAL mode, so a `.backup` copies a point-in-time consistent
snapshot without stopping the service:

```bash
mkdir -p backups/$(date +%Y%m%d-%H%M%S)
for db in audit intent_log cost_attestation ham; do
  sqlite3 "data/${db}.sqlite" ".backup 'backups/$(date +%Y%m%d-%H%M%S)/${db}.sqlite'"
done

# Journal + jsonls + keys are append-only or static — straight cp
cp data/journal.bin data/*.jsonl backups/$(date +%Y%m%d-%H%M%S)/
cp -r keys/        backups/$(date +%Y%m%d-%H%M%S)/
```

### Restore procedure

```bash
docker compose down
rm -rf data/ keys/
cp -r backups/<timestamp>/data backups/<timestamp>/keys ./
docker compose up -d
```

After restore, run the health check from §4 — if `chain_valid` comes
back true for every AID, the restore is good.

### Retention

* `data/journal.bin` and `*.jsonl` files **grow forever** — they are
  the audit-of-record. Plan for ~1 KB per `/evaluate` call.
* SQLite databases also grow but are queried often; consider
  `VACUUM` quarterly if they get large.
* For long retention, archive old journal segments to cold storage
  with their key fingerprint logged separately.

---

## 7. Health monitoring

### Liveness

```bash
curl -sf localhost:8000/healthz | jq
```

Returns `{ok, version, burn_in_id}`. Use as a Kubernetes liveness probe.

### Readiness signals

* `/attestation` should always return 200 with a valid Ed25519
  signature. If it doesn't, the signing key is missing or corrupt.
* `/forensic/replay` should always return `available: true` and
  `tampered_count: 0`. Anything else is a critical alert.
* `/burnin-status` should show layer slots progressing through
  phases over days/weeks. If they're stuck in `observation` for a
  layer that's seeing thousands of samples, the metrics aren't being
  fed (no `/burnin/label` calls).

### Dashboards

The web dashboard at `/` is human-facing and reads from the same
endpoints. For Grafana / Datadog, scrape:

| Metric | Source |
|---|---|
| Verdict counts by decision | Count `/audit/{aid}` records by `decision` |
| Active quarantines | `len(/admin/aid response.quarantined)` |
| Journal tampered count | `/forensic/replay.tampered_count` |
| Cost divergence escalations | Custom — instrument `aegis.cost.escalation` |
| Burn-in phase distribution | `/burnin-status` |
| Per-AID chain head age | Diff `now - max(records.signed_at_ns)` per aid |

---

## 8. Common production incidents

| Symptom | Likely cause | First action |
|---|---|---|
| Spike in BLOCK rate from one AID | Compromised agent or new tool not in whitelist | `GET /admin/aid/{aid}` → check violation reasons |
| `chain_valid: false` on `/audit/{aid}` | Disk corruption or process kill mid-write | Restore from latest backup; check `data/audit.sqlite-shm` and `-wal` |
| Costs exceeding forecast 3×+ | Model API price change or runaway agent | `GET /cost-attestation/{aid}` → compare divergence metrics |
| `/forensic/replay` returns `tampered_count > 0` | Bit-flip on disk, or attacker tried to edit `journal.bin` | Snapshot offline; do not restart; follow §4 |
| All endpoints return 503 | Auto-created paths failed (read-only filesystem?) | Check container volume mounts; verify `./data/` and `./keys/` are writable |
| sLLM judge taking > 5s | Anthropic API latency / rate limit | Falls back to `dummy` automatically; check Anthropic console |
| `mypy` / `ruff` failing in CI | Schema drift from a M-series upgrade | Pin the milestone tag; re-sync `cost_estimate` shape (M8 break) |

---

## 9. Upgrading to a new milestone

Each `feat(...)` commit on `main` corresponds to a milestone. To
upgrade safely:

1. **Read the commit message** for that milestone — it lists the
   external-shape changes.
2. **Run the test suite** against the new commit:
   ```bash
   git checkout <commit-sha>
   uv sync
   uv run pytest -q
   ```
3. **Snapshot `data/` and `keys/`** before deploying.
4. **Deploy.** All M8–M16 changes are additive at the storage layer
   — no migrations required. The one breaking change (M8's
   `cost_estimate` shape) is a request-body change, not a
   storage-format change.
5. **Verify** by running `demo/agent_demo.py` against the new
   container.

---

## 10. Decommissioning

If you're shutting down a deployment:

1. `docker compose down`
2. Final backup per §6.
3. **Wipe the keys**: `shred -u keys/*.pem keys/*.pub keys/*.key`
   — without the keys, the journal is opaque ciphertext. This is the
   patent's intended forget-properly path.
4. Archive the wiped-key journal alongside its public key fingerprint
   in cold storage. The journal cannot be decrypted without the data
   key, but the chain can still be **verified as continuous** by
   anyone holding the public key (the signature is over the
   ciphertext + AAD).
