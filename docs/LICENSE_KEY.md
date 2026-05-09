# License-Key Validation — Design

> **Status:** design only (2026-05-09). No license-key code is in the
> repo today; this document describes how the validation will work
> when we ship the Solo Pro / Team / Enterprise gate.

The contract this design has to satisfy:

1. **Offline validation.** Aegis is a privacy-preserving local tool.
   The Solo Free contract promises *0 outbound network requests* by
   default. Adding a "phone home to validate the license" step to
   the *paid* tiers would still poison the brand for everyone, so
   we don't.
2. **Tamper-evident.** A user can't edit the key file to flip
   `tier=pro` and unlock paid features.
3. **Revocable.** If a key leaks publicly we can stop honoring it
   without breaking honest users — even with offline validation as
   the default.
4. **Solo Free is unaffected.** Solo Free remains the default,
   keyless install.

---

## 1. Key shape

A license key is a single **Ed25519-signed JSON token**, base64-url
encoded, written to `~/.aegis/license.jwt`. The structure is JWS
(JSON Web Signature, RFC 7515) with a Ed25519 public key — same
crypto we already use for the audit chain, so no new dependencies.

Header:

```json
{
  "alg": "EdDSA",
  "typ": "JWT",
  "kid": "aegis-license-2026"
}
```

Claims:

```json
{
  "iss": "https://license.aegisdata.example",
  "sub": "user_01HRXY...",
  "aud": "aegis-mvp",
  "tier": "pro",                     // free | pro | team | enterprise
  "iat": 1762675200,
  "exp": 1794211200,                 // 1 year out by default
  "license_id": "lic_01HRXY...",     // for revocation
  "seats": 1,                        // pro=1, team=N, enterprise=custom
  "features": [                      // explicit allowlist; defense in depth
    "advisor.full",
    "judge.haiku",
    "embedding.bge-local",
    "audit.remote-backup"
  ],
  "burnin_bind": null                // optional — see §4
}
```

The signing key (`aegis-license-2026`) is held offline by the issuing
service. Its **public key ships in the binary** at
`src/aegis/license/keys.py` so any installed Aegis can verify any
license without a network call.

---

## 2. Validation flow

```
aegis-mvp startup
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  1. Read ~/.aegis/license.jwt (if absent → Solo Free)       │
│  2. Verify Ed25519 signature against pinned public key      │
│     (mismatch → Solo Free + warn user)                      │
│  3. Check `exp` against system clock                        │
│     (expired → Solo Free + warn)                            │
│  4. Check `aud == "aegis-mvp"` and `tier in {pro,team,...}` │
│  5. Optional: check burnin_bind matches local Burn-in id    │
│     (mismatch → Solo Free + warn — license is for another   │
│     machine)                                                │
│  6. Optional: check revocation list (CRL) — see §3          │
│  7. Activate `tier` features per the manifest in §5         │
└─────────────────────────────────────────────────────────────┘
```

If any step fails the runtime degrades to Solo Free. **Failure is
silent in the success path** (no banner spam) but logged to
`~/.aegis/license.log`. `aegis license status` prints the active
tier + the reason any failed checks landed.

---

## 3. Revocation

A leaked or refunded key has to stop working without us breaking the
"no phone home" promise for honest users. Three layers:

**Layer A — Short expiry.** Default `exp` is 1 year. A leaked Pro
key has at most 12 months of damage before it expires by itself.
We rotate `kid` annually so even an undetected leak times out.

**Layer B — Optional online CRL.** Solo Pro / Team users can opt
into `aegis license refresh` (manual command). It downloads a tiny
JSON CRL (Certificate Revocation List) signed by the same key and
caches it at `~/.aegis/license-crl.json`. The runtime consults the
cached CRL on every startup. **The opt-in is explicit** — on a
fresh install, no automatic outbound is made.

**Layer C — Burn-in bind.** Enterprise keys can be bound to a
specific machine's Burn-in id (the SHA3-256 of the running Aegis
binary measurement). This makes the key non-portable. Solo Pro is
explicitly **not** burn-in-bound — Pro users move between laptops.

---

## 4. Feature manifest

The runtime side (Python) maps `tier` to a feature set. The
*license claims may also list explicit `features`* — both must agree
or the most-restrictive wins (defense in depth against a forged
claim that adds a feature the tier doesn't normally include).

```python
# src/aegis/license/features.py (planned)
TIER_FEATURES = {
    "free": frozenset(),
    "pro": frozenset({
        "advisor.full",
        "judge.haiku",
        "judge.phi35",
        "embedding.bge-local",
        "audit.remote-backup",
    }),
    "team": TIER_FEATURES["pro"] | frozenset({
        "sidecar.multi-tenant",
        "atmu.2pc",
        "report.cross-cuts",  # --by-aid-and-provider, etc.
        "slack-connect",
    }),
    "enterprise": TIER_FEATURES["team"] | frozenset({
        "ham.tee-bind",
        "audit.aes-gcm-journal",
        "audit-patrol",
        "cost-attestation.dual-key",
        "compliance.evidence-packaging",
    }),
}
```

A function the rest of the codebase calls:

```python
from aegis.license import has_feature

if has_feature("advisor.full"):
    run_advisor_pipeline()
else:
    # Solo Free — advisor stays OFF, no banner
    pass
```

`has_feature` returns `True` for Solo Free for everything in the
free set, and falls through to `False` for paid features without
shouting about it. The user opted into Solo Free deliberately —
nagging them with upsell modals would violate the contract.

---

## 5. What the license never affects

Hard-coded in the runtime, not touched by the feature manifest:

- The 16-step firewall pipeline runs on every tier
- The Ed25519 audit chain runs on every tier
- `aegis verify-audit` works on every tier (so an expired-license
  user can still prove their historical audit chain is intact)
- Rule and playbook updates ship to every tier
- Security patches ship to every tier

The feature gate is *additive features only*, never *security
removals*.

---

## 6. CLI surface

```bash
aegis license activate <key.jwt>   # write to ~/.aegis/license.jwt
aegis license status               # print tier + expiry + checks
aegis license deactivate           # remove + revert to Solo Free
aegis license refresh              # opt-in CRL fetch (Layer B)
aegis license verify <key.jwt>     # offline check w/o activating
```

`aegis license status` example output:

```
tier:        pro
license_id:  lic_01HRXY...
seats:       1 (1 used)
expires:     2027-04-30 (354d remaining)
signature:   verified (kid=aegis-license-2026)
crl:         cached 2026-05-09; not in revocation list
burnin_bind: not set (any machine)
features:    advisor.full, judge.haiku, judge.phi35,
             embedding.bge-local, audit.remote-backup
```

---

## 7. Open questions (TBD before implementation)

- Trial keys: bound to the Burn-in id of the laptop generating
  them? (Yes — but UX needs to be one command: `aegis trial start`.)
- Multi-machine Pro: how does a single seat work when an engineer
  uses laptop + desktop + work-laptop? Probably "any 3 active in
  rolling 90 days" is fine. Low-stakes since a Pro key is cheap to
  re-issue.
- Refund window: 30 days, no questions asked? Aligns with stripe
  default. The runtime side doesn't need to know — the issuer just
  revokes.
- Offline-only deployment (Enterprise air-gapped): the issuer ships
  a signed bundle on a USB stick, including the license + a CRL
  snapshot. Refresh cadence by physical media every quarter.

---

## 8. Why this design and not …

**Why not a simple `~/.aegis/license-pro.flag` file?** Trivially
forgeable. We already use Ed25519 for the audit chain — using JWS
keeps the cryptographic surface uniform.

**Why not require online activation per launch?** Violates the
"no outbound by default" contract that the README sells. We are
deliberately giving up some anti-piracy strength in exchange for
keeping the privacy posture clean.

**Why not encrypt the license with the user's password?** Doesn't
help — anyone who can run Aegis on the machine can read the key
file. Tamper-evidence comes from the signature, not from secrecy.

**Why not GitHub Sponsors / per-organization licensing?** Possible
later for Enterprise. For Solo Pro / Team, per-key is simpler and
matches how engineers actually use the tool.

---

## 9. Implementation plan

| Step | Status | PR |
|------|--------|-----|
| 1. `src/aegis/license/keys.py` — pinned issuer public key | 🔴 not started | TBD |
| 2. `src/aegis/license/verify.py` — JWS Ed25519 verify + claims check | 🔴 not started | TBD |
| 3. `aegis.license.has_feature` + tier feature manifest | 🔴 not started | TBD |
| 4. CLI subcommands (`aegis license …`) | 🔴 not started | TBD |
| 5. Wire `--profile pro/cloud` to feature gate | 🔴 not started | TBD |
| 6. Wire 8-advisor pipeline to feature gate | 🔴 not started | TBD |
| 7. Wire sidecar mode to feature gate | 🔴 not started | TBD |
| 8. Issuer service (separate repo) | 🔴 not started | TBD |
| 9. Stripe / payment integration (separate repo) | 🔴 not started | TBD |

The runtime-side changes (steps 1–7) are ~1 week of work. The issuer
service and payments (steps 8–9) live outside the Aegis repo — what
matters for *this* repo is steps 1–7, and they happen after we have
≥3 paying design partners willing to test the activation flow.
