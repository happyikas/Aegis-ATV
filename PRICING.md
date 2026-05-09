# Aegis ATV — Pricing & Tiers

> **Where the free / paid boundary lies, written down before we ship a
> license-key gate so the contract is clear.** The Aegis-ATV repo is
> Apache-2.0; nothing in this document changes that. What this document
> defines is which **commercial offerings** sit alongside the OSS core
> and what they include.

**Status (2026-05-09):** Solo Free is live and unconditionally free
forever (Apache-2.0). Solo Pro / Team / Enterprise are in **design-
partner phase** — pricing below is the published rate that takes
effect once we exit design partner. Design partners get those tiers
free for the pilot window (see [`docs/DESIGN_PARTNER_PROGRAM.md`](docs/DESIGN_PARTNER_PROGRAM.md)).

---

## At a glance

| Tier | Price | Mode | Profile | Audit chain | Advisor | Support |
|------|-------|------|---------|-------------|---------|---------|
| **Solo Free** | $0 forever | `local` | `free` | local SQLite | OFF | community (GitHub) |
| **Solo Pro** | $19 / mo | `local` | `pro` or `cloud` | local SQLite + remote backup | ON (8-advisor) | email, 48 h |
| **Team** | $39 / seat / mo · min 5 seats | `local` or `sidecar` | any | shared sidecar audit DB | ON | email, 24 h, Slack Connect |
| **Enterprise** | custom | `sidecar` | any | shared + AES-GCM journal + AuditPatrol | ON + provider-divergence + per-channel baselines | dedicated CSE, 4 h SLA |

A single contract covers all engineers in the org for Team / Enterprise.

---

## What's free, forever

The full **Aegis ATV core** runs under Apache-2.0 with no functional
limits:

- 16-step ATV-2080-v1 firewall
- Ed25519 + SHA3 chained audit log (`~/.aegis/audit.jsonl`)
- 31 detection rules + 6 incident playbooks (`policies/rag_corpus/`)
- ATV Coach / Live / Doctor commands
- `aegis report`, `aegis verify-audit`, `aegis forensic`
- Multi-channel + multi-provider attribution (PR-D, PR #134/#136/#142)
- OpenClaw plugin (`@happyikas/openclaw-plugin-aegis`, npm,
  Apache-2.0)

You can run this in production indefinitely without a license key.
**The Solo Free tier is not a trial — it's the contract.**

What Solo Free does *not* include is anything that requires us to run
infrastructure on your behalf (remote backup, dashboards, notification
delivery) or expert hours (advisor tuning, custom playbooks, SOC 2
evidence packaging).

---

## Solo Pro — $19/mo

For an individual engineer who wants the upgrade path beyond local
laptop installation.

**On top of Solo Free:**

- `--profile pro` — local Phi-3.5-mini judge + bge-local embedding
  (~700 MB GGUF, downloaded once)
- `--profile cloud` — Anthropic Haiku judge for grey-zone calls
  (requires your own `ANTHROPIC_API_KEY`; we do not resell tokens)
- 8-advisor pipeline ON: kv-cache-optimizer, prompt-cache-canary,
  cost-divergence, security-reviewer, instruction-drift-canary,
  provider-divergence, channel-attribution, post-incident
- Encrypted off-laptop backup of `audit.jsonl` (S3-compatible bucket
  you own; we never see the records)
- License key valid for one engineer (any number of machines you
  personally use)
- Email support, 48 h SLA
- Updates and new playbook drops

**What it costs us to deliver:** support time + updates pipeline.
The judge / embedding models run on your hardware.

---

## Team — $39 / seat / mo (min 5 seats)

For a small team that wants a shared audit chain and one report for
the whole group.

**On top of Solo Pro:**

- `--mode sidecar` deployment — multi-tenant FastAPI + Postgres
  audit DB. All engineers' calls land in one queryable chain.
- ATMU 2-Phase Commit + compensation plans for irreversible tools
- `aegis report --by-aid`, `--by-channel`, `--by-provider`,
  `--by-aid-and-provider` — team-wide cross-cuts
- Slack Connect channel for the team
- 24 h support SLA
- Onboarding workshop (1 day, remote)

**Seat counting:** one seat per engineer who has Aegis installed
(local) or whose `aid` shows up in the sidecar audit log over a
billing month. You can over-provision; we don't audit your headcount.

---

## Enterprise — custom

For an engineering organization with compliance, cost-attestation,
or air-gapped deployment requirements.

**On top of Team:**

- HAM (Hardware Attestation Manifest) — TEE / TPM bind, Claim 26
- AuditPatrol — background re-validation of historical decisions,
  Claim 54
- AES-GCM encrypted journal (`docs/AUDIT_ROTATION.md`)
- Cost attestation profile + Claim 34 dual-key cost-divergence
  detection
- Custom playbooks tuned to your environment
- SOC 2 / EU AI Act / HIPAA / ISO 42001 evidence packaging
- Air-gapped deployment support (the OpenClaw + Local OSS LLM
  track — see [`docs/releases/OPENCLAW_LOCAL.ko.md`](docs/releases/OPENCLAW_LOCAL.ko.md))
- Dedicated customer success engineer
- 4 h support SLA, 24/7 for severity-1 incidents

Enterprise pricing depends on user count, deployment topology, and
compliance scope. Contact `sales@aegisdata.example` (or open an issue
labeled `enterprise-inquiry`) for a quote.

---

## License key

The Solo Free tier requires no key. Solo Pro / Team / Enterprise
ship a license key that the runtime validates locally — the same
laptop that runs Aegis must be able to validate the license offline,
because Aegis itself is a privacy-preserving local tool and we are
**not** going to introduce a phone-home requirement for a security
tool that promises "no data leaves your laptop".

Technical design: [`docs/LICENSE_KEY.md`](docs/LICENSE_KEY.md).

---

## What we will *not* charge for

A few principled commitments so the line is clear:

- **The OSS core** stays Apache-2.0. We will not relicense, even
  for paid tiers.
- **Bug fixes** ship to all tiers, including Solo Free. Security
  patches go out at the same time.
- **Audit log records you generated** stay yours forever — even if
  your subscription lapses. You will always be able to read your
  own `audit.jsonl` with the OSS `aegis verify-audit` tool.
- **Token resale** — we do not resell Anthropic / OpenAI / Google
  tokens. `--profile cloud` uses your own API key.
- **Per-incident pricing** — we charge per seat, not per incident
  detected. There is no incentive on our side to surface false
  positives.

---

## Discounts

- **Open-source maintainer discount** — 50% off Solo Pro for any
  maintainer of an OSS project with ≥1k GitHub stars. Email proof
  of maintainership.
- **Academic / non-profit** — Team tier at Solo Pro pricing.
- **Design partners** — see [`docs/DESIGN_PARTNER_PROGRAM.md`](docs/DESIGN_PARTNER_PROGRAM.md).
  3 slots, 30-day pilot, $0.

---

## FAQ

**Do I need a license key to run the firewall?**
No. Solo Free is the firewall, fully functional, zero gating.

**What happens if my license expires?**
The runtime falls back to Solo Free. You keep all your audit
records. You lose `--profile pro/cloud`, the 8-advisor pipeline,
and remote backup. No work is destroyed.

**Can I try Pro / Team before paying?**
Yes — every paid tier has a 30-day full-feature trial that requires
no credit card. The trial license key is generated by `aegis login`
and is bound to your laptop's burn-in id (no account creation).

**Do you offer a perpetual / one-time-purchase Solo Pro?**
Not yet. We may once we have predictable update cadence; today the
update pipeline is irregular and a perpetual license would be
mispriced.

**Do you train models on my prompts?**
No. We do not see your prompts. The local audit log stays on your
laptop; the only thing the sidecar (Team / Enterprise) sees is the
ATV-2080-v1 vector representation, which is intentionally not
reversible to natural-language prompts.

---

## Change log

* **2026-05-09** — initial draft. Numbers are aspirational until we
  exit design partner. The free / paid *boundary* is committed.
