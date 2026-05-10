# Inference Registry — `~/.aegis/inference.toml`

> **Gap B (issue #145).** Multi-agent + multi-LLM deployments need
> per-agent visibility into the inference backend each agent is using.
> The registry lets `aegis metrics --all` and
> `aegis report --by-aid-and-provider --with-live` produce per-agent
> live-metrics views without the operator hand-running scrapes.

The single-endpoint surface (`aegis metrics --vllm-url …`) is fine
when one Aegis instance front-runs one vLLM server. When an OpenClaw
deployment runs multiple agents each pointing at a different
backend — or a mix of vLLM + cloud — you want the *aid* dimension on
the live data the same way `--by-aid-and-provider` puts it on the
audit data.

---

## Schema

Default location: `~/.aegis/inference.toml`. Override via
`AEGIS_INFERENCE_REGISTRY=<path>`.

```toml
# Optional defaults — apply to every endpoint that doesn't override.
[defaults]
timeout_s = 2.0

# vLLM endpoint — has /metrics, fully scrapeable.
[endpoints.agent-a-bot]
provider     = "vllm"
metrics_url  = "http://10.0.0.10:8000/metrics"

[endpoints.agent-b-reviewer]
provider     = "vllm"
metrics_url  = "http://10.0.0.20:8000/metrics"
timeout_s    = 5.0    # override default

# Cloud provider — recorded for attribution but not scraped.
# `provider_name` is purely a human label.
[endpoints.agent-c-research]
provider       = "cloud"
provider_name  = "anthropic-claude-3-5"

# Disabled endpoint — kept in config for documentation / quick
# re-enable, but `aegis metrics --all` skips it.
[endpoints.agent-d-archive]
provider     = "vllm"
metrics_url  = "http://10.0.0.30:8000/metrics"
enabled      = false
```

The aid (the bracket name) **must match the `aid` field** that the
OpenClaw plugin or local hook stamps onto each ATV record. That's
how `--with-live` joins the audit data with the live metrics.

### Allowed `provider` values

| Value | Status | Behavior |
|-------|--------|----------|
| `vllm` | shipped | Prometheus `/metrics` scrape; `metrics_url` required |
| `cloud` | shipped | Recorded for attribution only; not scraped |
| `ollama` | reserved | Recorded as skipped; adapter PR pending |
| `tgi` | reserved | Recorded as skipped; adapter PR pending |

### Validation

A malformed `inference.toml` does **not** brick the runtime — the
firewall keeps working. A registry-level error surfaces at
`aegis metrics --all` time as a single yellow `[metrics]
inference.toml: …` line on stderr, exit 1. Specifically rejected:

- Unknown `provider` tag
- `provider = "vllm"` without `metrics_url`
- Non-positive `timeout_s` (per-endpoint or in `[defaults]`)
- Duplicate `[endpoints.<aid>]` block
- Wrong types (`metrics_url` not a string, etc.)

A missing file is **not** an error — Aegis just falls back to the
single-endpoint surface.

---

## Commands

```bash
# Default (single endpoint, legacy) — unchanged.
aegis metrics --vllm-url http://localhost:8000

# Multi-endpoint (Gap B) — scrapes every enabled endpoint
# concurrently. Cloud / disabled endpoints render "skipped".
aegis metrics --all

# Single endpoint by registry label.
aegis metrics --aid agent-a-bot

# JSON output for jq / fleet-monitor pipes.
aegis metrics --all --json

# Cross-reference live metrics in the multi-agent report.
aegis report --by-aid-and-provider --with-live
```

`--with-live` adds one `live: KV=87.0%  queue=2/0  band=high` line
under each aid block in the report. Aids in the audit log that
aren't in `inference.toml` render `live: not in registry` so the
operator can see the gap. Unreachable endpoints render
`live: unreachable (reason)`.

---

## How the runtime treats unreachable endpoints

A scrape failure is **not fatal**. The firewall is a security gate;
live metrics are a best-effort observation. When `vllm:/metrics` is
slow, down, or returns a non-200, the multi-scrape orchestrator
turns the failure into a typed `EndpointUnreachable` result:

```json
{
  "kind": "unreachable",
  "aid": "agent-b-reviewer",
  "metrics_url": "http://10.0.0.20:8000/metrics",
  "reason": "vLLM /metrics timed out after 5.0s …",
  "endpoint_unreachable": 1
}
```

The `endpoint_unreachable: 1` flag is the canonical signal a
downstream consumer (advisor, dashboard, alerting rule) reads.

---

## Concurrency

`aegis metrics --all` uses a `ThreadPoolExecutor` capped at 16
workers. Real fleets are well under that; the cap is defensive
against socket exhaustion if someone registers a very large number
of endpoints. Pool size is `min(N_endpoints, 16)`; tests cover the
extreme of `max_workers=0` (clamped up to 1).

---

## Roadmap

| Status | Feature |
|--------|---------|
| ✅ shipped | vLLM scrape, multi-endpoint registry, `--all` / `--aid`, `--with-live` |
| 🟡 planned | Ollama adapter (issue TBD) |
| 🟡 planned | TGI adapter (issue TBD) |
| 🔴 deferred | DCGM / `nvidia-smi` GPU metrics for environments without DCGM |

See [`ROADMAP.md`](../ROADMAP.md) for the surrounding multi-agent +
multi-LLM follow-on (Gap C — per-(aid, provider) baseline learning,
Gap D — inter-agent edge tracking).
