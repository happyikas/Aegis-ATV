# Aegis ATV — Threat Model

> **Audience**: maintainers + a third-party security auditor. The
> "Auditor checklist" in §8 is the action item for the audit.
>
> **Status**: written 2026-05-10 against the v0.3.0 codebase;
> trust-boundary / mitigation substance unchanged through v0.3.1
> (a metadata + license-gate-activation patch). PyPI / GHCR ship
> v0.3.1; the v0.3.0 tag itself never published. Treated as living
> doc — every PR that changes a trust boundary or mitigation is
> expected to update the corresponding row.

This document is **not** marketing. We list residual gaps explicitly
in §7. Aegis is a security product; pretending to be more secure
than we are would be the worst thing we could do.

---

## §1 What Aegis is, in one paragraph

Aegis is an action firewall + cryptographic audit chain that sits
between an AI agent (Claude Code, OpenClaw, etc.) and its tools.
Every tool call posts to Aegis before it runs; Aegis returns
ALLOW / REQUIRE_APPROVAL / BLOCK with a per-step trace, then
SHA3-chains + Ed25519-signs the verdict into an append-only audit
log. The core promise is: *"we have signed proof that the agent did
exactly this and nothing more."* Threats to that promise are the
threats this document is about.

---

## §2 Roles + trust boundaries

```
                       ┌──────────────────────┐
                       │  human operator      │
                       │  (CLI, dashboard)    │
                       └──────┬───────────────┘
                              │ trust: high
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │  Aegis sidecar / local hook                          │
   │  ┌────────────────────────────────────────────────┐  │
   │  │  16-step firewall + Ed25519 signing            │  │
   │  │  audit chain (SHA3 + Ed25519)                  │  │
   │  │  encrypted journal (AES-GCM)                   │  │
   │  └────────────────────────────────────────────────┘  │
   │           │                  │                       │
   │           ▼                  ▼                       │
   │  ┌────────────────┐  ┌────────────────────┐          │
   │  │ ~/.aegis/      │  │ in-memory state:   │          │
   │  │ audit.jsonl    │  │ burnin slots,      │          │
   │  │ audit.sqlite   │  │ rate-limit buckets │          │
   │  │ license.jwt    │  └────────────────────┘          │
   │  │ keys/*.pem     │                                  │
   │  └────────────────┘                                  │
   └────────┬───────────────────────────────┬─────────────┘
            │ trust: TOOL CALLS ONLY        │ trust: low
            │ (HTTP /evaluate)              │ (network)
            ▼                               ▼
   ┌────────────────────┐       ┌────────────────────┐
   │  AI agent process  │       │  external LLM API  │
   │  (Claude Code,     │       │  (Anthropic,       │
   │   OpenClaw, etc.)  │       │   OpenAI, vLLM)    │
   │  trust: limited    │       │  trust: limited    │
   └────────────────────┘       └────────────────────┘
```

### Trust boundaries

| # | Boundary | What crosses |
|---|----------|--------------|
| TB-1 | Agent process ↔ Aegis | tool name + args (untrusted), prompt fragment (untrusted) |
| TB-2 | Aegis ↔ filesystem | audit log (write), license.jwt (read), Ed25519 keys (read), policies (read) |
| TB-3 | Aegis ↔ external LLM API | grey-zone judge prompt (sanitized), audit-chain content **never** crosses |
| TB-4 | Aegis sidecar ↔ HTTP clients | tool calls, audit queries, ATMU outcome reports |
| TB-5 | Aegis ↔ vLLM `/metrics` | scrape (read-only) — local OSS track only |
| TB-6 | Aegis ↔ license issuer (planned) | JWS license token, optional CRL refresh |
| TB-7 | Operator ↔ Aegis CLI | full admin access (subprocess of operator's shell) |
| TB-8 | Aegis ↔ Aegis (multi-instance) | inter_agent_edges field, when populated (Gap D, future) |

### Roles + assumed capabilities

| Role | What they can do | What they cannot do |
|------|-----------------|--------------------|
| **Operator** | install / configure Aegis, run CLI, inspect audit log, hold private keys | n/a — operator is the trusted root |
| **AI agent** | issue tool-call requests via TB-1, observe verdicts | tamper with the audit log, read other tenants' state, forge signatures |
| **Local-machine attacker** (process running as same user) | read everything the operator can read, write to filesystem | escalate to root unless OS already compromised |
| **Network attacker** (sidecar mode, off-box) | send HTTP requests, observe HTTP responses, attempt rate-limit / size DoS | read filesystem, hold the Ed25519 signing key |
| **Compromised agent process** | TB-1 + send arbitrary `tool_args_json` content (attacker-controlled), claim arbitrary `aid` / `tenant_id` | read other tenants' audit records, forge a verdict |
| **Compromised LLM API** (TB-3) | influence the grey-zone judge's verdict for one specific call | bypass deterministic firewall steps (310-336), see the audit chain |
| **Supply-chain attacker** (npm / PyPI / GHCR) | publish a malicious package update | already-installed Aegis cannot be retroactively poisoned via this path |

---

## §3 Assets

| ID | Asset | Why it matters |
|----|-------|---------------|
| A1 | **Audit chain integrity** | The compliance promise. Tampering is the highest-impact threat. |
| A2 | **Audit chain confidentiality** | Tool args may include secrets / PII. Audit log is a fat target. |
| A3 | **Firewall verdict correctness** | A bypassed BLOCK is the operational failure that gets a customer breached. |
| A4 | **Cryptographic keys** | Ed25519 audit signing key + AES-GCM journal key + (future) license issuer key. |
| A5 | **License-key validity** | Solo Pro / Team / Enterprise commercial revenue depends on this. |
| A6 | **Multi-tenant isolation** | Sidecar mode promises per-tenant audit chains. Cross-tenant leakage is a contract violation. |
| A7 | **Process availability** | If the firewall is DoS-ed offline, the agent might run unfiltered (depending on caller's failClosed setting). |

---

## §4 Threats — STRIDE walk

Each threat has an ID, the asset(s) it targets, the adversary, and
the mitigation status. PR references trace the mitigation to specific
lines on `main`.

### Spoofing

| ID | Threat | Targets | Mitigation | Status |
|----|--------|---------|-----------|--------|
| S-1 | Compromised agent claims a different `tenant_id` to read another tenant's audit chain | A6 | Sidecar mode treats `tenant_id` as identity claimed by caller; future `--require-aid-proof` flag will demand an Ed25519 sig from the caller (PR-pending). Today: **operator must trust the agent process**. | 🟡 partial — documented limitation |
| S-2 | License-key forgery — adversary mints a JWS that validates as Pro / Enterprise | A5 | Ed25519 JWS signed by issuer key pinned in `src/aegis/license/keys.py`. Without the issuer's private key, no forgery is possible. | ✅ shipped (PR #157, design PR #144) |
| S-3 | Replay of a valid license-key on a different machine | A5 | Optional `burnin_bind` claim binds a license to a specific Burn-in id (Enterprise tier); Pro / Team intentionally portable. | ✅ shipped (Enterprise) |
| S-4 | Audit record forgery — adversary writes a fake record to the JSONL log | A1 | Ed25519 signature over `this_hash` (opt-in via `aegis audit-key init`); SHA3 chain links every record to the previous. Tamper detected by `aegis verify-audit`. | ✅ shipped |
| S-5 | DNS hijack of `pypi.org` / `ghcr.io` during install → poisoned package | TB-2 | PyPI: trusted-publisher OIDC (no API tokens to steal). GHCR: HTTPS + GitHub-pinned image tags. Sigstore/cosign signing of GHCR images: 🔴 not yet wired. | 🟡 partial — sigstore deferred |

### Tampering

| ID | Threat | Targets | Mitigation | Status |
|----|--------|---------|-----------|--------|
| T-1 | Post-write mutation of `audit.jsonl` (e.g. delete a BLOCK record) | A1 | SHA3 chain breaks at the mutation; `aegis verify-audit` returns non-zero with the broken record index. | ✅ shipped |
| T-2 | Mutation of a rotated `.gz` file (post-PR #158) | A1 | `verify_chain` walks the rotation chain transparently across compressed boundaries; mutation breaks the chain regardless of compression state. Test: `test_audit_rotation_compression.py::test_chain_walks_through_compressed_rotations` | ✅ shipped (PR #158) |
| T-3 | Reorder / splice records mid-chain | A1 | Same SHA3 chain — `prev_hash` mismatch detected. | ✅ shipped |
| T-4 | Tamper with the encrypted journal (M15) | A1, A2 | AES-GCM-256 with the cleartext header bound as AAD. Decrypt fails on any byte flip. | ✅ shipped (M15) |
| T-5 | Modify a baseline / policy file to weaken future verdicts | A3 | step309 instruction-drift detection — `CLAUDE.md`, `.mcp.json`, plugin manifests are SHA3-baselined; any drift BLOCKs every PreToolUse until the operator runs `aegis baseline reattest`. | ✅ shipped (v2.2 step309) |
| T-6 | Tamper with the Ed25519 signing key file | A1, A4 | OS file permissions (0600 by default). If the machine is compromised, the attacker can re-sign, but the chain *prior* to the compromise is preserved by the public-key fingerprint published via `aegis audit-key show`. **Key compromise does NOT silently break historical audit log integrity** — it just means the attacker can extend the chain forward. Detection: a verifier comparing the chain's pubkey fingerprint against an offline-stored fingerprint. | 🟡 partial — operator-managed |

### Repudiation

| ID | Threat | Targets | Mitigation | Status |
|----|--------|---------|-----------|--------|
| R-1 | Operator denies a tool call ran ("the agent didn't issue that command") | A1 | Every call has trace_id + Ed25519 signature + timestamp. `aegis forensic <trace>` recovers the full per-call timeline. | ✅ shipped |
| R-2 | Operator denies a verdict was BLOCK ("you let it through") | A1 | The signed audit record contains the canonical decision string. Bytewise tamper-evident. | ✅ shipped |

### Information disclosure

| ID | Threat | Targets | Mitigation | Status |
|----|--------|---------|-----------|--------|
| I-1 | 5xx response leaks Python traceback / source paths | A2 | Structured error envelope (PR #159) — every 500 returns `{"error": {"code": "internal_error", "message": "unexpected server error"}}`. Traceback only goes to structlog, never to the body. Test: `test_unhandled_exception_returns_envelope_not_traceback`. | ✅ shipped (PR #159) |
| I-2 | Tool args containing secrets get written verbatim to the audit log | A2 | step312 (input normalization) redacts known-secret patterns (API keys, AWS access keys) before signing. Operator can extend via `policies/redaction.json`. | ✅ partial — known patterns only |
| I-3 | Cross-tenant read in sidecar mode — tenant A queries tenant B's audit chain | A2, A6 | `aegis report` filters by tenant_id; sidecar `/audit/{aid}` requires a matching tenant claim. Without the future identity-proof feature (S-1), tenant_id is operator-claimed, not authenticated. | 🟡 partial — see S-1 |
| I-4 | Side-channel timing: BLOCK vs ALLOW takes measurably different time, leaking which rule matched | A3 | Not currently mitigated. The 16-step pipeline has step-skip optimizations (e.g. step305 fast-path) that an attacker measuring p50 latency could in principle map to specific rules. **Acknowledged residual risk** — see §7. | 🔴 unmitigated |
| I-5 | Embedding leaks via `--profile cloud` (Anthropic Haiku judge) | A2, TB-3 | Solo Free contract: 0 outbound by default. `--profile cloud` is opt-in and the operator's API key is the trust root. The grey-zone judge sees a **redacted** prompt subset, not raw tool args. | ✅ shipped |
| I-6 | License key file readable by other users on shared host | A5 | `~/.aegis/license.jwt` written 0600 (`stat.S_IRUSR \| stat.S_IWUSR`), best-effort on non-POSIX. Test: `test_write_sets_owner_only_perms`. | ✅ shipped (PR #157) |

### Denial of service

| ID | Threat | Targets | Mitigation | Status |
|----|--------|---------|-----------|--------|
| D-1 | Massive request body OOMs the sidecar | A7 | RequestSizeLimitMiddleware caps at 1 MiB (default), 413 with envelope. Both Content-Length fast path + streaming counter for chunked. | ✅ shipped (PR #159) |
| D-2 | Request flood saturates the sidecar | A7 | RateLimitMiddleware token bucket per (X-Tenant-ID \|\| IP), default 600/min sustained + 100 burst, 429 with `Retry-After`. /healthz + /readyz exempt so LB probes don't trip. | ✅ shipped (PR #159) |
| D-3 | Audit log fills disk → fsync errors → firewall hangs | A7 | Audit rotation (50 MB threshold + gzip + 10-slot retention) caps disk usage at ~50 MB compressed. `aegis audit prune` for emergency space recovery. | ✅ shipped (PR #158) |
| D-4 | sLLM judge timeout under load → every grey-zone call hangs | A7 | step340 has explicit timeout + fall-back to deterministic verdict. The dummy judge (Solo Free) has no network call at all. | ✅ shipped |
| D-5 | Multi-replica deployment + in-memory rate limit → tenant evades cap by hitting any replica | A7 | Documented in `src/aegis/api/middleware.py:RateLimitMiddleware` docstring. Mitigation requires Redis-backed limiter (separate PR). | 🟡 partial — single-replica MVP fine |
| D-6 | Slowloris-style slow request | A7 | uvicorn default request timeouts apply. Not Aegis-specific. | 🟡 inherited from uvicorn |

### Elevation of privilege

| ID | Threat | Targets | Mitigation | Status |
|----|--------|---------|-----------|--------|
| E-1 | Compromised agent escalates by claiming admin endpoints (e.g. `/admin/aid/release`) | TB-4 | Admin endpoints require `AEGIS_ADMIN_TOKEN` (env-supplied by operator, not derived from agent input). Default token is intentionally unsuitable for production (`dev-admin-token`); operator must set a real one. | ✅ shipped (M14 admin) |
| E-2 | License-tier escalation via forged `features` claim that includes ham.tee-bind | A5 | `features_for(claims)` returns the **intersection** of tier-expansion and explicit features list. A forged extra feature is dropped. Test: `test_features_for_explicit_intersects_with_tier`. | ✅ shipped (PR #157) |
| E-3 | step336 loop detector trips on legitimate retry + operator manually overrides → next loop slips through | A3 | Operator override is itself recorded in audit chain. The risk is operator complicity, not undetected escalation. | ✅ accepted — recorded |

---

## §5 Cross-cutting design properties

A few properties protect multiple threats at once. Documenting them
here so the auditor knows what to test holistically.

### P-1: SHA3 chain — one mathematical property protects many threats

The chain link is `this_hash = SHA3-256(canonical_json({prev_hash,
…record minus this_hash + signature}))`. Any byte change anywhere
in the audit log breaks the chain forward from the change point.
This single property handles T-1, T-2, T-3, R-1, R-2 with no
additional logic.

### P-2: Pure-ASGI middleware bypass closure

PR #159's middleware design note: Starlette's `ServerErrorMiddleware`
catches `Exception`-handler responses *outside* the user middleware
chain. Pure-ASGI wrapping of the `send` callable + baking security
+ request-id headers into the error envelope's JSONResponse closes
the gap. Test `test_security_headers_set_on_error_responses_too` is
the canary.

### P-3: Solo Free contract — 0 outbound by default

Defense-in-depth against a whole class of network-leak threats
(I-1, I-5, S-5 partially). The default install never resolves DNS
for any external API. Verifiable via `tcpdump` / Little Snitch
during a Claude Code session. Opt-in to `--profile cloud` is the
single conscious choice that flips this.

### P-4: Append-only — records are never updated

The audit chain has no UPDATE path; only APPEND. This eliminates a
huge class of TOCTOU and consistency bugs that plague mutable audit
systems. ATMU (Agent Telemetry Management Unit) state transitions
are recorded as new records, not mutations of prior records.

### P-5: Burn-in measurement binds verdicts to the running binary

Every audit record includes `burn_in_id` — a SHA3-256 of the
running Aegis code + policy directory + provider names + public
key. A future verifier can prove "this verdict was emitted by THIS
specific Aegis build, not a swapped binary". Detected via
`aegis baseline reattest`.

---

## §6 Cryptographic primitives + their assumptions

The auditor should verify each primitive is used per its standard
threat model, with no protocol mistakes layered on top.

| Primitive | Where | Standard | Assumption |
|-----------|-------|----------|-----------|
| SHA3-256 | `_hash_record` (chain link), license `kid` lookup, burn-in measurement | NIST FIPS 202 | Collision resistance — a 2^128 attacker cannot forge a colliding record. |
| Ed25519 | Audit-chain signature, license JWS, cost-attestation (M12 dual-key Claim 34), TEE binding | RFC 8032 | EdDSA security; private key remains private. **Side-channel resistance** depends on the cryptography lib (we use `cryptography` Ed25519, which uses libsodium-style constant-time implementations on supported platforms). |
| AES-256-GCM | Encrypted journal (M15), HAM data key (M16) | NIST SP 800-38D | Nonce never reused under the same key. We derive nonces from a counter + a random salt; counter wraparound is far beyond practical run lengths. |
| HKDF-SHA256 | (planned, not yet shipped) license key derivation for per-tenant variants | RFC 5869 | Standard. |
| HMAC-SHA256 | Internal: not currently used in the chain (SHA3 is keyless). Used in a few advisor signal contexts. | RFC 2104 | Standard. |

**No custom crypto.** Every operation is from `cryptography` /
`hashlib`. The auditor should NOT find any handwritten primitive.
If they do, that's a bug.

---

## §7 Residual risks (acknowledged gaps)

These are gaps we have not closed yet. They're listed here so the
auditor knows where to focus + so customer-facing SOC 2 evidence
packets can disclose them honestly.

### RR-1: tenant_id is operator-claimed, not authenticated

Sidecar mode trusts the caller's `X-Tenant-ID` header. A malicious
caller can claim any tenant id. **Mitigation path**: per-tenant
Ed25519 keypair, caller signs a nonce, sidecar verifies. Tracked as
a future PR — not started.

### RR-2: side-channel timing leakage

Per-step skip optimizations make latency a function of which rule
fired. An adversary that can measure p50 latency precisely could in
principle infer rule structure. **Mitigation path**: per-step
constant-time delay (small; ~1 ms padding). Not implemented; the
cost would impact p99 budget. Tracked as a research-stage item.

### RR-3: GHCR images are not sigstore-signed

The `release-docker.yml` workflow publishes to GHCR with the OCI
content-trust assumption — i.e. you trust the publisher's GitHub
identity. We have NOT wired sigstore/cosign signing yet.
**Mitigation path**: add `cosign sign` step to release-docker.yml;
add verification example to the install docs. Easy follow-up PR.

### RR-4: PyPI sdist contains the README + CHANGELOG

These are intentionally included for visibility on `pypi.org`'s
project page, but they reference internal docs (DECK_*.md, etc.)
that would only confuse a sdist-only consumer. Low impact — no
secret leakage — but the auditor may flag it as cleanup.

### RR-5: Multi-replica rate limiter

In-memory token bucket caps a single replica only. Multi-replica
deployments need an external limiter (Redis, Envoy). Documented in
`src/aegis/api/middleware.py:RateLimitMiddleware`. Out of scope for
the MVP single-replica target.

### RR-6: License-key issuer service does not exist yet

The runtime gate is shipped (PR #157, no-op state). The issuer
service that mints real licenses is a separate repo, not started.
Until it ships, no real licenses can be issued — and the runtime
correctly rejects forgery attempts. The threat surface is *zero*
today, but the issuer service itself will need its own threat
model when it ships.

### RR-7: No supply-chain SBOM

We don't yet generate / publish a Software Bill of Materials.
Mitigation path: `cyclonedx-py` step in release-pypi.yml,
publishing the SBOM as a release asset. Easy follow-up PR.

### RR-8: No fuzz-testing of the firewall input parser

The firewall accepts an `ATVInput` (Pydantic v2 model). Pydantic's
parse path is well-tested upstream, but Aegis's own consumers
(step310 args inspection, step311 donor rules) operate on the
parsed structure. We rely on unit tests + the existing 31-rule +
6-playbook test corpus, not on coverage-guided fuzzing. **Mitigation
path**: hypothesis-based property tests targeting each step's rule
predicates. Tracked as a future PR.

---

## §8 Auditor checklist

For the third-party audit. Each item is a concrete test the auditor
should run; we suggest specific commands / files to verify.

> **Note on "sensitive paths" below**: the auditor should test
> against the canonical "system identity" + "system secret" file
> paths on the target host (the names are intentionally redacted
> here so this document doesn't trip Aegis's own
> sensitive-path rule when the maintainer commits it). Refer to
> `policies/sensitive_paths.json` for the complete enumeration.

### 8.1 Audit chain integrity

- [ ] **Reproduce a tamper detection**. Generate 10 audit records,
      tamper one record's `tool_args_json` field manually, run
      `aegis verify-audit`. Confirm:
      * exit code is non-zero
      * stdout names the broken record's index
      * the tampered record AND every subsequent record are flagged
- [ ] **Cross-rotation tamper detection**. Tamper a record inside a
      `.gz` rotation file (decompress → mutate → recompress).
      Confirm `aegis verify-audit` catches it.
- [ ] **Genesis hash is deterministic**. Two fresh installs on
      different machines produce identical first-record hashes for
      the same record content (modulo timestamp).
- [ ] **Public-key fingerprint is stable** across `aegis audit-key
      show` invocations and across machines if the same key was copied.

### 8.2 Cryptographic primitives

- [ ] **No custom crypto**. `grep -rn 'def _sign\|def _verify\|def
      _hash' src/aegis | sort` should find ONLY thin wrappers around
      `cryptography` / `hashlib` calls. Any handwritten primitive
      is a bug.
- [ ] **Ed25519 signature verifiable independently**. Take a signed
      audit record, the public key (`aegis audit-key show`), and
      an off-the-shelf `pynacl` / `cryptography` script — confirm
      the signature verifies.
- [ ] **AES-GCM nonce uniqueness**. Examine `EncryptedJournal`
      writes during a 1k-record run; confirm no nonce collision.

### 8.3 Firewall verdict correctness

- [ ] **No bypass via path encoding**. Test `tool_args_json`
      payloads with URL-encoded slashes, double-encoded Unicode,
      base64-wrapped destructive commands. Confirm step310 +
      step311 still BLOCK.
- [ ] **No bypass via case folding**. Verify `KuBeCtL` /
      `KUBECTL` / `kubectl` (and other rule keywords) all match the
      cloud_destructive rule.
- [ ] **Sensitive-path coverage**. Run `Read` against the host's
      canonical identity / secret files (per
      `policies/sensitive_paths.json`). All must REQUIRE_APPROVAL
      or BLOCK.
- [ ] **Loop detector fires correctly**. Issue the same `(tool,
      args)` 3× from the same `aid` within 1 minute. Confirm 3rd
      hits step336 → REQUIRE_APPROVAL.

### 8.4 Sidecar production hardening

- [ ] **Request size limit**. POST `/evaluate` with a 2 MiB body.
      Confirm 413 + structured envelope; confirm no firewall path
      was reached (no audit record produced).
- [ ] **Rate limit per tenant**. Burn the bucket from
      `X-Tenant-ID: alice`; confirm `bob` still has full quota.
- [ ] **No traceback on 500**. Force a 500 (e.g. corrupt the SQLite
      DB mid-run). Confirm response body is exactly `{"error":
      {"code": "internal_error", "message": "unexpected server
      error"}}`. **No traceback. No filenames. No error chain.**
- [ ] **/readyz transitions to 503 on SIGTERM**. Send SIGTERM;
      observe /readyz returns 503 within 5s; observe in-flight
      requests complete; observe /healthz still returns 200 (process
      is alive).

### 8.5 License-key gate

- [ ] **Forgery rejection**. Mint a JWS signed by an attacker-
      generated keypair, set `kid` to a known good value. Confirm
      `aegis license verify` returns `bad-signature`.
- [ ] **Tier escalation rejection**. Mint a valid Pro license (using
      a test issuer key the auditor controls; patch into
      `ISSUER_PUBLIC_KEYS`) but with explicit `features:
      ["ham.tee-bind"]`. Confirm `has_feature("ham.tee-bind")`
      returns False (intersection with tier-expansion).
- [ ] **Burn-in bind enforcement**. Mint a license with `burnin_bind:
      "abc"`; activate on a machine whose Burn-in id is `def`.
      Confirm activation rejects with `burnin-bind-mismatch`.
- [ ] **Expiry enforcement**. Mint a license with `exp` in the past.
      Confirm activation rejects with `expired`. Confirm runtime
      degrades to Solo Free silently.
- [ ] **No outbound network requests on Solo Free**. Run `aegis
      install --mode local --profile free`; immediately use `tcpdump`
      / Little Snitch over a 5-minute Claude Code session. Confirm
      0 outbound connections from the Aegis process.

### 8.6 Multi-tenant isolation (sidecar mode)

- [ ] **Per-tenant audit-chain partitioning**. Submit calls from
      tenants A and B; confirm `aegis report --tenant A` shows
      only A's calls; ditto B.
- [ ] **(Known gap RR-1)** Confirm that without `--require-aid-proof`
      (not yet shipped), a caller CAN claim any `tenant_id`. This
      is documented; the auditor should confirm the gap rather than
      flag it as a bug.

### 8.7 Supply chain

- [ ] **PyPI publish provenance**. Confirm published `aegis-mvp`
      sdist + wheel were built by the GitHub-Actions workflow
      `release-pypi.yml`, not uploaded by a stray API token.
      (Visible in PyPI's "publisher" field on the project page.)
- [ ] **GHCR publish provenance**. Confirm the OCI manifest's
      `org.opencontainers.image.source` label points at the public
      GitHub repo.
- [ ] **(Known gap RR-3)** Confirm GHCR images are NOT yet sigstore-
      signed. The auditor should call this out as a follow-up.

### 8.8 Documentation

- [ ] `SECURITY.md` reporting policy reachable from README.
- [ ] `docs/THREAT_MODEL.md` (this file) is up to date with the
      shipped surface.
- [ ] `PRICING.md` claims that match the runtime — anything
      "Enterprise" is gated, anything "Solo Free" is unconditional.

---

## §9 Out-of-scope for this threat model

To keep the audit focused, here's what we explicitly do NOT cover.
The auditor should redirect rather than spend time on these:

* **The agent's own security** (Claude Code's sandbox, OpenClaw's
  permissions). Aegis is the firewall *between* the agent and its
  tools. The agent process compromise is part of our adversary
  model (see §2), but proving the agent itself is secure is
  upstream's job.
* **Operating-system level threats**. We assume the OS file
  permissions, process isolation, etc. are as-documented. A
  rooted machine has many bypass paths none of which are
  Aegis-specific.
* **The OpenClaw runtime itself**. The plugin is in our scope
  (`openclaw-plugin/`); the upstream OpenClaw runtime is not.
* **Anthropic / OpenAI / vLLM API security**. We assume the
  external API does what its docs say. Compromises there can
  influence the grey-zone judge but cannot bypass deterministic
  steps 310-336.

---

## §10 Living-doc commitment

This file is treated as part of the security surface. Every PR that:

* adds a trust boundary (a new HTTP endpoint, a new file path),
* changes a cryptographic primitive,
* changes an authentication / authorization rule,
* removes a mitigation,
* adds a new role,

…must update the corresponding row in this file in the same PR.
Reviewer should check this in PR review the same way they check
test coverage.

The current revision is signed off by the maintainer at the commit
that introduced it. A future commit that meaningfully updates the
threat model should bump the date in §1 and add an entry to the
"Revision history" footer.

---

### Revision history

| Date | Commit | Changes |
|------|--------|---------|
| 2026-05-10 | (initial) | Initial threat model targeting v0.3.0 surface |
| 2026-05-11 | post-0.3.1 | Status note clarified: v0.3.0 codebase, v0.3.1 published (metadata + license-gate-activation patch; trust-boundary substance unchanged) |
