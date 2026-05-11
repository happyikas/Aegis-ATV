# Show HN submission copy

> File this at `news.ycombinator.com/submit`. HN strips most
> formatting — bold, italic, headers, etc. all collapse to plain text.
> Indented two-space lines render as `<pre>` blocks (use sparingly).
> Links in the body work but render as plain blue text.
>
> **Last refreshed: 2026-05-09** — bodies and titles updated to
> reflect the npm-published OpenClaw plugin, 16-step pipeline,
> multi-agent cross-grouping, and Apache-2.0 license. The original
> draft (Apr 2026) is preserved at the bottom under "Archive — earlier
> draft" because the comment-thread prep there is still mostly valid.

---

## Title (≤ 80 chars)

**Pick one.** Each is under 80 chars. Order is rough preference; the
first three lead with the most-recognizable hook (Claude Code) for
HN's coding-tools-fluent audience.

| Title | Chars |
|---|---|
| Show HN: A cryptographic audit chain for every Claude Code tool call | 71 |
| Show HN: Action firewall for AI agents — every tool call signed and chained | 76 |
| Show HN: 16-step firewall + Ed25519 audit log for AI agent tool calls | 70 |
| Show HN: Aegis — Apache-2.0 firewall for Claude Code + OpenClaw agents | 70 |
| Show HN: Wrap every AI tool call in a 2080-D vector before letting it run | 75 |
| Show HN: Tamper-evident audit chain for autonomous coding agents | 65 |

Avoid: emoji starts, "production-ready", "AI-powered" prefixes,
internal milestone labels (T2 / M16 / etc.). HN is allergic to all
four.

---

## URL field

Point at the repo root. The README's lead asset is the demo GIF, so
the first thing a clicker sees is a 25-second screencast.

```
https://github.com/happyikas/Aegis-ATV
```

If you want the demo GIF to load even faster, link directly:
```
https://github.com/happyikas/Aegis-ATV#aegis--action-firewall-for-claude-code
```

---

## Body — Version C (refreshed 2026-05-09, recommended)

Target ~1,800 chars; HN truncates around 3,000. Lead with the
cryptographic audit chain because that's the differentiating feature
no other tool in this category has, then the 16-step firewall, then
the multi-agent cross-grouping (the most recent work), then the
honest "what's still preview" footer.

```
Aegis is an Apache-2.0 sidecar that adds a cryptographic audit chain
and a 16-step firewall to every tool call your AI agent makes. I
built it because Claude Code's --allowedTools is a static allowlist
with no audit trail, and `--dangerously-skip-permissions` is the
opposite of what I want at 2am.

Every tool call posts to a Python sidecar before it runs. Output:

  ALLOW / BLOCK / REQUIRE_APPROVAL
    + per-step trace
    + Ed25519 signature
    + SHA3-256 chain link to the previous record

Stored append-only at ~/.aegis/audit.jsonl. `aegis verify-audit`
walks the chain in one command and exits non-zero on any mutation
(edits, deletes, re-orderings — all caught).

The 16-step pipeline (ATV-2080-v1):

  args inspection → identity → blast radius → instruction drift →
  loop detector → cost gate → policy + sLLM judge → approval →
  sign + chain → exec

The whole call gets lifted into a fixed 2,080-D vector with 30 named
subfields, so the sLLM judge returns structured per-subfield
attribution ("blocked 60% on prompt_injection, 30% on tool_call")
instead of a hopeful prose explanation.

Three release tracks share the same firewall + chain:

  - Claude Code (GA): `aegis install --target claude-code`
  - OpenClaw + Cloud LLM (GA): npm install
    @happyikas/openclaw-plugin-aegis — multi-channel
    (Telegram/Discord/Slack/CLI), multi-provider (Claude/GPT/Gemini)
  - OpenClaw + Local OSS LLM (GA): air-gapped, vLLM/Ollama,
    no outbound network

What lit up most recently: `aegis report --by-aid-and-provider`
cross-groups the audit chain by (agent, LLM provider) and surfaces
"this agent's BLOCK rate diverges 5× across the providers it has
used" — the canary for vendor-migration safety drift.

Stack: 2,369 tests passing on Python 3.11/3.12/3.13. mypy strict
clean. Runs in one Docker container. Solo Free tier is unconditionally
free forever (Apache-2.0); paid tiers are in design-partner phase
(see PRICING.md for the boundary).

Honest scope: the OpenClaw plugin's E2E test against the real Aegis
sidecar landed last week (PR #143); the runtime contract is solid
but the OpenClaw runtime side itself isn't yet npm-published by
upstream, so that integration is end-to-end tested only against a
plugin-side simulation of the runtime.

Comments, criticisms, prior-art pointers all welcome. The patent
this implements is full of working software, not handwaving — happy
to get into the technical claims thread if anyone wants.
```

**Char count: ~2,200.** Closer to the upper bound than I'd like,
but the multi-track preview status really does need explanation —
HN will catch any sleight-of-hand about preview-vs-GA.

---

## Body — Version B (refreshed; lead with the why)

For a more philosophical thread. Stays under 1,500 chars.

```
Every time I leave Claude Code unattended on a long task I have the
same thought: what is actually preventing this thing from doing
something stupid? The honest answer is "a regex denylist in the
system prompt that the model itself enforces." That is bad.

So I built a pre-commit firewall: a Python sidecar that every tool
call posts to before it runs. It returns ALLOW / BLOCK /
REQUIRE_APPROVAL with a signed reason, keeps a tamper-evident audit
chain so I can answer "what did the agent actually try?" three
months later, and exposes a dashboard where I watch verdicts in
real time.

The architecture lifts every tool call into a fixed 2,080-D vector
with 30 named subfields. That shape unlocks structured per-subfield
attribution from the sLLM judge ("blocked 60% on tool_call, 30% on
prompt_injection") and statistical drift detection per subfield
through a 5-layer Burn-in baseline.

Solo Free tier is unconditionally free forever under Apache-2.0
(the cryptographic audit chain, the 16-step pipeline, all of it).
Paid tiers add Phi-3.5/Haiku judge upgrades and a multi-tenant
sidecar for teams; pricing is in PRICING.md.

2,369 tests pass, mypy strict over the source tree, runs in one
Docker container. Demo GIF + dashboard screenshots in the repo.

I'm posting looking for: prior art I should cite, threat models
I missed, and patches.

Code: github.com/happyikas/Aegis-ATV
```

**Char count: ~1,500.**

---

## Comment-thread prep

HN top-comment patterns to anticipate. Drafted replies (post these
yourself, ideally within the first hour while the post is hot).
Numbers refreshed 2026-05-09 against the current main branch.

### "How is this different from OPA / Gatekeeper / [existing policy engine]?"

> OPA evaluates policies, which is most of step340 in this firewall.
> What's bolted on top is: (a) a fixed-shape 2080-D vector
> representation of the call so the sLLM judge can return structured
> per-subfield attribution; (b) Ed25519 + SHA3 chaining of every
> verdict so you can independently re-verify the audit log; (c) the
> 30-day forensic journal under AES-GCM with the cleartext header
> bound as AAD — flip a bit anywhere and decrypt fails; (d) per-AID
> circuit breaker that auto-quarantines an agent after N violations;
> (e) ATMU 2-phase commit so the firewall record and the tool's
> actual outcome are bound. OPA is part (a); the other five layers
> are the contribution.

### "Why a sidecar and not a library?"

> Library means in-process, trust-the-caller. Sidecar means the
> firewall has its own signing key, its own audit DB, and its own
> attack surface — the agent process can be compromised without the
> firewall being compromised. Same reason envoy is a sidecar and not
> a library. We *also* ship a `--mode local` in-process hook for the
> Solo Free path where deployment simplicity matters more than
> isolation; the schema is identical so you can graduate from local
> to sidecar without touching your audit chain.

### "Why Ed25519 and not [post-quantum]?"

> Ed25519 today, with a Claim-25 ML-DSA stub waiting for when NIST's
> post-quantum slot stabilizes. The codebase has the layering for
> dual-signing; it's just not enabled by default because PyNaCl /
> cryptography don't have ML-DSA upstream yet. The audit chain's
> SHA3-256 link is hash-only and post-quantum-safe today.

### "Show me the Haiku prompt."

> src/aegis/judge/haiku.py. SYSTEM_PROMPT requires a JSON object with
> verdict + confidence + attribution (a dict[subfield_name,
> contribution_score] over the 30 named subfields). Falls back to a
> deterministic rule-based dummy judge if ANTHROPIC_API_KEY is
> absent. The Solo Free contract guarantees zero outbound network
> requests — `--profile cloud` opts you into Haiku explicitly.

### "How many tests, what's the coverage?"

> 2,369 tests on Python 3.11/3.12/3.13, mypy strict clean over the
> source tree. Coverage on aegis.* is ~85% per pytest --cov; the
> gaps are mostly in the hardware-band placeholders (200-D zero-fill
> in T2) and the dummy providers, which are tested by being
> substituted into integration tests rather than directly. The
> OpenClaw plugin has 31 vitest unit tests + 6 E2E tests against
> a real Aegis sidecar (PR #143).

### "Have you actually used this against a real workload?"

> Yes — installed as a Claude Code PreToolUse hook on my Mac mini,
> firewalling my own dev work for ~4 weeks. Anecdotally caught: a
> "rm -rf node_modules" that was meant to be `rm -rf
> ./packages/foo/node_modules`; an accidentally-committed AWS access
> key I tried to grep for via `cat ~/.aws/credentials`; and a
> `kubectl delete ns prod` that the model generated when asked to
> "clean up the test namespace" (the firewall caught it on the
> cloud_destructive rule, no harm done). The dogfood report is at
> docs/DOGFOOD.md / docs/DOGFOOD_PHASE_B.md.

### "Won't this slow down my agent?"

> /evaluate p50 is ~12ms with the dummy providers, ~180ms with
> Haiku judge in the policy-deferred path (which fires only when
> the regex/blast/cost gates haven't already decided). Most calls
> hit one of the cheap fast-path stages. The audit append is
> synchronous but in WAL mode, so contention is bounded. The
> step305 safe allowlist (Read/Grep/Glob, plus a curated bash
> subcommand list) skips the LLM judge entirely for known-safe
> ops — under 5ms.

### "Open core or rugpull-bait?"

> Open core, no rug. The 16-step firewall, the audit chain, the
> sLLM judge, the OpenClaw plugin — all Apache-2.0, fully functional
> on Solo Free without a license key. The paid tiers (Solo Pro,
> Team, Enterprise) add features that require us to run
> infrastructure on your behalf (remote backup) or expert hours
> (advisor tuning, compliance evidence packaging). Bug fixes and
> security patches ship to all tiers. PRICING.md commits the
> boundary publicly. Your audit log records always remain readable
> with the OSS verifier even if your subscription lapses.

### "What about OpenClaw — is that real?"

> The Aegis side is real and Apache-2.0. The OpenClaw plugin is
> published as @happyikas/openclaw-plugin-aegis on npm with 31
> vitest tests and 6 E2E tests against a real Aegis sidecar.
> OpenClaw the agent runtime itself is in development by upstream
> (not by me). Until the runtime is npm-published, the plugin is
> labeled `-preview` and the integration is verified against a
> plugin-side simulation of the runtime contract — honest about
> the scope, see CHANGELOG.md.

### "Why a patent?"

> The patent isn't mine to hold — I read it (link in repo) and
> built one possible T2 software reduction. The IP belongs to its
> author; the code is Apache-2.0. Worth reading the patent itself;
> it's well-written and the claims map to specific files in the
> source tree (`docs/PATENT_SUPPLEMENT_v3.md` has the cross-ref).

---

## Timing

* **Best slot**: Tuesday or Wednesday, 8–10am US Pacific.
  HN's median weekday peak engagement is ~10am PT; Show HN posts
  catch the morning skim crowd.
* **Avoid**: Friday afternoon (drains over the weekend), Monday
  morning (everyone's too busy), the day of any major Apple/Google
  keynote.
* **Stay around**: HN expects the author to answer questions in the
  thread for the first 4–6 hours. Don't post and ghost.

---

## Pre-flight checklist

Before submitting (mirrors issue [#151](https://github.com/happyikas/Aegis-ATV/issues/151)):

- [ ] Demo GIF current (the README hero asset; reflects current CLI surface)
- [ ] `aegis verify-audit` works against a fresh install in <60 seconds
- [ ] The "first 30 seconds" path in README is accurate (no broken commands)
- [ ] `npm install @happyikas/openclaw-plugin-aegis` works from a fresh shell (resolves to 0.3.0 via `latest`)
- [ ] PRICING.md is published so HN commenters can answer "is this open core or rugpull-bait?"
- [ ] At least 1 design partner reference (anonymized OK) we can link to
- [ ] Author has 4–6 hours blocked off after submission

---

## What to do after submitting

1. **Don't ask anyone to upvote.** HN detects this and will silently
   rank your post lower or kill it outright. Tweet the URL once,
   plainly, with no "please upvote" language.
2. **Respond to comments thoughtfully.** Even hostile ones. The
   comment thread is where Show HN posts win or lose visibility.
3. **Pin the post to your GitHub README** (the badge pattern below).
4. **Save the comment archive** for your own records.

---

## Optional GitHub README badge

Add to the very top of `README.md` (above the demo GIF):

```markdown
[![Discussed on Hacker News](https://img.shields.io/badge/HN-discussed-orange)](<HN URL after posting>)
```

Grab the badge URL from `shields.io/badge` and update once the post
is live.

---

## Archive — earlier draft (April 2026)

Preserved for the comment-thread prep nuggets that are still valid
but predate the current API surface. Title candidates and body
versions A/B from this draft are superseded by Version C above.

### Old title set

| Title | Chars |
|---|---|
| Show HN: Action firewall for AI agents — every tool call signed and chained | 76 |
| Show HN: I built an Ed25519-signed firewall for AI agent tool calls | 67 |
| Show HN: 7-stage firewall for AI agents, with a tamper-evident audit chain | 75 |
| Show HN: AegisData T2 — patent-aligned firewall sidecar for AI agents | 70 |
| Show HN: Wrap every AI tool call in a 2080-D vector before letting it run | 75 |

The "7-stage" framing is stale (current pipeline is 16 steps). The
"AegisData T2" framing leads with internal milestone vocabulary
that HN doesn't recognize. "Wrap every AI tool call in a 2080-D
vector" still works as a hook and is preserved as candidate #5 in
the refreshed set above.

### Body — Version A (April 2026, leads with what)

```
This is a Python sidecar that sits between your AI agent (Claude Code,
Cursor, a homegrown LangChain thing) and its tools. Every tool call goes
through a 7-stage firewall before it runs:

  arg inspection → AID auth → blast radius → human gate →
  cost forecast → policy + sLLM judge → approval → sign+chain → exec

The verdict (ALLOW / BLOCK / REQUIRE_APPROVAL) plus a per-stage trace
gets Ed25519-signed and Merkle-chained into a tamper-evident audit log,
and also AES-256-GCM-encrypted into a separate forensic journal where
the cleartext header is used as AAD — flip a bit anywhere and decrypt
fails.

What surprised me building it:

- Lifting the tool call into a fixed 2,080-D vector (30 named
  subfields) makes the sLLM judge return per-subfield attribution
  scores. Explanations become structured output, not hopeful strings.

- The patent (40 claims) is full of working software, not handwaving.
  Claim 34 says cost-attestation signing keys must be distinct from
  telemetry signing keys; the codebase has two separate Ed25519 keys
  and two separate ledgers. Claim 27 says cost-divergence escalation
  runs independently of the sLLM verdict; mine does, and the test
  suite proves it.

- The hardest bug was a re-entrant lock in the per-AID circuit
  breaker. Three methods all wanted to snapshot state under a single
  threading.Lock; the fix was the snapshot-under-lock pattern with a
  private helper. Tests under thread contention pass.

326 tests, mypy strict over 61 files, runs in one Docker container.

60-second install: docs/QUICKSTART.md
Per-milestone source tour: docs/ARCHITECTURE.md
Pre-rendered demo GIF + 9 dashboard screenshots:
  demo/recording/README.md

Comments, criticisms, prior-art pointers all welcome.
```

The technical anecdotes here (Claim 34 dual-key, Claim 27
independence, the threading.Lock bug) are still relevant if a
commenter goes deep — quote them inline as needed in the live thread.

### Old comment-thread sections

The earlier sections "Why a sidecar and not a library?", "Why
Ed25519 and not [post-quantum]?", "Have you actually used this?",
"Won't this slow down my agent?", "Why a patent?" — all preserved
in spirit in Version C's comment-thread prep above, with refreshed
numbers (2,369 tests not 326; Apache-2.0 not "MIT [or whichever]").
