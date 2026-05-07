# Security policy

Aegis is a security tool. Bugs that let an unsafe tool call slip past the
firewall, or that let an attacker forge / tamper with the audit chain,
are treated as critical.

## Reporting a vulnerability

**Please do not file a public GitHub issue for security bugs.**

Instead, email the maintainer at the address listed in the GitHub
profile of [@happyikas](https://github.com/happyikas), with subject
prefix `[aegis-security]`. If you prefer encrypted disclosure, request
the maintainer's PGP key in the same message and we'll move to email.

Include:

* A short description of the issue and impact.
* A minimal reproduction (PoC, payload, or unit test).
* Aegis version (`uv run aegis --version` or commit SHA).
* Whether you'd like public credit and under what name.

We aim to:

* Acknowledge within **48 hours**.
* Provide a triage update within **7 days**.
* Ship a fix or mitigation within **30 days** for high-severity issues
  (firewall bypass, audit forgery, key extraction). Lower-severity
  issues may take longer; we will keep you informed.

## In scope

| Area | Examples |
|------|----------|
| **Firewall bypass** | Crafted tool args that should match a rule but don't; encoding / unicode / whitespace tricks; PreToolUse hook reaching `ALLOW` for a known-destructive op. |
| **Audit chain forgery** | Inserting / mutating / removing records without `aegis verify-audit` failing; Ed25519 signature trivially forgeable; Merkle chain advance without prev-hash check. |
| **Key extraction** | Reading the firewall's signing private key from disk under expected filesystem permissions; leaking key bytes through structured logs or error messages. |
| **Sandbox / privilege escalation** | Hook process gaining capabilities beyond what `aegis install` requested; injecting commands via env / argv parsing. |
| **Self-DoS** | A pathological input that wedges PreToolUse so that legitimate tool calls cannot proceed (different from intentional REQUIRE_APPROVAL gates). |
| **Sensitive-data leak** | Local mode making any unintended cloud / network call; embedding non-redacted secrets into audit log payloads or telemetry vectors. |

## Out of scope

* Issues against the **dummy provider** that exist *because it is the
  dummy provider* (e.g., trivially fooled by paraphrase). The dummy
  provider is intentionally rule-based and ships as a no-cloud
  fallback. Real-judge / real-embedding modes are in scope.
* `localhost:8000` Sidecar exposure on a multi-tenant box (the
  service is not designed to be exposed to untrusted local users).
* Demo-only assets: `demo/`, `tests/fixtures/`, `screens/*.png`.
* Third-party dependencies — please report those upstream first;
  we'll mirror once a CVE is assigned.

## Coordinated disclosure

We follow a **90-day default embargo** from the date of acknowledged
report. Earlier public disclosure may be agreed for low-impact issues
or already-public details. We will credit reporters in the release
notes and (with permission) the README "Security" section.

## Severity guidance

We use a simple rubric, in order of priority:

1. **Critical** — silent firewall bypass on a step310/311 dangerous
   pattern, or audit chain forgery without verify-audit failing.
2. **High** — bypass requires unusual but reachable input; key
   extraction under default install; cloud-mode unintended egress.
3. **Medium** — local DoS / hang under crafted input; verbose error
   path leaks non-secret context.
4. **Low** — documentation / hardening suggestion; cosmetic.

## What we will not do

* We will not file legal action against good-faith researchers who
  follow the disclosure process above.
* We will not ask you to delete your PoC after public disclosure.
* We will not silently patch — security fixes are called out in the
  changelog and release notes.
