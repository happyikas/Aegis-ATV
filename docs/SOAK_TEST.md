# Soak / Load Test Runbook

> **Production sign-off**. Run this before declaring a deployment
> ready for paying customers. The 24h duration is intentional —
> shorter runs miss memory leaks, audit-log rotation correctness
> under sustained load, and the slow-burn p99 latency degradation
> that only shows up after a few hours.

---

## When to run

* **Once per release tagged `v*`** before you publish to PyPI / GHCR.
* **Once per hardware refresh** (the operator's deploy environment
  changed — different CPU, different filesystem, different OS).
* **Before signing the first paying design partner**. Don't deliver
  Solo Pro / Team to a customer until you've personally watched a
  24h soak go green on the same hardware shape they'll run.

You don't need to repeat for every PR — that's what `aegis bench`
(5-minute smoke) is for, and CI runs it on every push.

---

## Hardware target

The harness is single-machine. Sufficient for the rates Aegis
realistically sees in MVP deployments:

| Soak target rate | Generator host needs |
|------------------|----------------------|
| ≤ 50 RPS | any laptop |
| 50–500 RPS | dedicated host with 4+ vCPU, 8 GB RAM |
| > 500 RPS | distributed harness — out of scope here |

The **target sidecar** can be on the same machine as the harness
(less realistic, but cheap) or on a separate machine (preferred —
isolates the load generator's CPU from the sidecar's). Production-
shape soaks should be cross-machine.

---

## Pass/fail criteria

The harness evaluates four thresholds:

| Threshold | Default | Override |
|-----------|---------|----------|
| **Error rate** | < 1% | `--max-error-rate 0.005` |
| **p99 latency** | < 500 ms | `--max-p99-ms 250` |
| **Audit chain integrity** | clean at all checks | `--no-chain-check` to skip |
| **Throughput floor** | off | `--min-throughput 9.0` (e.g. for 10/s target) |

"Error" = HTTP 5xx, network failure, **or** decision mismatch
(firewall returned `ALLOW` for what the harness expected to be
`BLOCK`, or vice versa). A decision mismatch indicates a regression
in firewall semantics under load — those count as errors.

The harness exits non-zero on any threshold violation, so a CI
pipeline can wrap `aegis bench` in a quality gate.

---

## Running it

### Smoke (CI / dev loop)

```bash
# 5 min, 50 RPS, single localhost target. Fits a CI job's 10-min budget.
aegis bench --target http://localhost:8000
```

### Full 24h sign-off

```bash
# Start the sidecar separately (preferably on a different host).
# Then run from the generator host:
aegis soak \
  --target https://aegis.your-deployment.example \
  --duration 24h \
  --rate 10/s \
  --concurrency 16 \
  --max-error-rate 0.005 \
  --max-p99-ms 250 \
  --output /var/log/aegis/soak-$(date +%Y-%m-%d).json
```

Expected artifacts at completion:

* stdout: human-readable PASS/FAIL summary
* `--output` JSON: machine-readable detail (every metric + per-check
  audit chain status). Ingest into your fleet monitor.

The JSON shape is stable; documented in
``aegis.soak.SoakResult.to_json``.

---

## What to monitor *outside* the harness

The harness measures the *sidecar's response*. It does **not**
monitor the sidecar's host metrics. Watch separately:

| Metric | Why | How |
|--------|-----|-----|
| **RSS / process memory** | Memory leak detection. Should stabilize within ~1h and stay flat. | `ps -o rss -p $(pgrep -f aegis.main)` every 5min, or your fleet monitor |
| **Disk usage on `~/.aegis/`** | Audit log + rotation behaving correctly. Should plateau once retention kicks in. | `du -sh ~/.aegis/` periodic |
| **CPU %** | Saturation indicator. >80% sustained → you're at the ceiling. | `top` / cgroup metrics |
| **File descriptors** | Leaky connections. | `ls /proc/$(pgrep -f aegis.main)/fd \| wc -l` periodic |
| **Audit log growth** | Confirms rotation triggered. Compare against `AEGIS_AUDIT_MAX_BYTES`. | `aegis audit status --json` periodic |

Recommended: run a small companion script that captures these every
60s and emits a JSONL log. The full soak's "did anything go wrong"
postmortem hinges on having both the harness output AND host metrics.

---

## Common failure modes

### "p99 latency 1200ms > max 500ms"

Three usual suspects:
1. **Disk-bound audit append**. Look for SSD vs HDD; check the
   filesystem (ext4 / APFS / ZFS all have different fsync costs).
   Mitigation: enable group-commit (`AEGIS_JOURNAL_GROUP_COMMIT=true`).
2. **GC pause storm**. Python's gen-2 GC kicks in around hour 2-4
   under sustained allocation. Mitigation: `gc.set_threshold(...)`
   tuning (separate PR).
3. **Concurrency cap on the sidecar**. uvicorn's default worker
   count (1) becomes the bottleneck. Run with `--workers 4` minimum.

### "error_rate 0.034 > max 0.01"

Look at `decisions` + `decision_mismatches` in the JSON. If
mismatches are non-zero, that's a firewall regression — capture
the audit log and file a bug. If mismatches are zero but errors
are 5xx, the sidecar crashed mid-soak — check `journalctl` /
docker logs for the stack trace.

### "chain verify failed at t=21600s"

Audit chain broke during the soak. Critical — this is the exact
case the chain integrity check is designed to catch. File a bug
with:
* The output JSON
* `~/.aegis/audit.jsonl` from the moment of failure (preserve
  before any restart)
* The full set of rotated files (`audit.jsonl.{1..K}.gz`)

This has never been seen in a healthy run; if you see it, the
sidecar has a serious bug worth pausing the release for.

---

## Reproducibility

The harness is deterministic given:

* Same `--seed` (default 42)
* Same payload mix
* Same target sidecar version

Two runs against the same sidecar with the same flags should
produce identical *requested* sequences. Latency measurements vary
with the host's load, but error counts + decision counts should
match exactly.

This makes it possible to bisect a regression: if a soak fails on
v0.4.0 but passes on v0.3.9, the same `--seed` lets you replay
the exact request sequence against either build.

---

## What this runbook does NOT cover

* **Distributed load** beyond ~500 RPS. The harness is single-host;
  high-rate distributed load would need a follow-up PR (probably
  switching to k6 or locust as a separate optional dep).
* **Failure injection** (chaos engineering — kill -9 the sidecar
  mid-soak, fault-inject the audit DB, etc.). Out of scope. Run
  the soak against a happy-path sidecar first; chaos comes after.
* **Multi-tenant isolation under load**. The default mix uses one
  tenant. Add `--seed` variants and rerun if you want to verify
  the rate-limit middleware's per-tenant isolation under contention.

---

## Sign-off template

When a 24h soak passes, attach the following to the release PR:

```
Soak sign-off — v0.X.Y
======================
Hardware:        <CPU, RAM, disk type>
Sidecar host:    <hostname or "same machine">
Generator host:  <hostname>
Started:         2026-MM-DDTHH:MM:SSZ
Ended:           2026-MM-DDTHH:MM:SSZ
Result:          PASS
  error rate:    0.0023  (max 0.01)
  p99:           182ms   (max 500ms)
  throughput:    9.97/s  (target 10/s)
  chain checks:  144 passed / 144 total
  decisions:     allow=606234 approval=129892 block=129872

Output JSON:    <link to artifact>
Host metrics:   <link to graphs>
```

A signed-off soak result lands in `docs/launch/soak-runs/` for the
auditable record.
