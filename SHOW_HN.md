# Show HN submission copy

> File this at `news.ycombinator.com/submit`. HN strips most
> formatting — bold, italic, headers, etc. all collapse to plain text.
> Indented two-space lines render as `<pre>` blocks (use sparingly).
> Links in the body work but render as plain blue text.

---

## Title (≤ 80 chars)

**Pick one.** Each is under 80 chars. The first three are most likely to land.

| Title | Chars |
|---|---|
| Show HN: Action firewall for AI agents — every tool call signed and chained | 76 |
| Show HN: I built an Ed25519-signed firewall for AI agent tool calls | 67 |
| Show HN: 7-stage firewall for AI agents, with a tamper-evident audit chain | 75 |
| Show HN: AegisData T2 — patent-aligned firewall sidecar for AI agents | 70 |
| Show HN: Wrap every AI tool call in a 2080-D vector before letting it run | 75 |

Avoid: "AegisData T2 MVP" (too internal), anything with "production-ready"
(HN allergy), anything starting with an emoji.

---

## URL field

Point at the repo root. The README's lead asset is the demo GIF, so the
first thing a clicker sees is a 25-second screencast.

```
https://github.com/happyikas/Aegis-ATV
```

If you want the demo GIF to load even faster, link directly:
```
https://github.com/happyikas/Aegis-ATV#aegisdata-t2-mvp
```

---

## Body (target ~1,500 chars; HN wraps at ~3,000)

Two versions. Pick based on whether you want to lead with the **what**
or the **why**.

### Version A — leads with the what (recommended for "Show HN")

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

**Char count: ~1,650.** Posts under ~2,000 chars get more eyeballs
than longer ones; HN front-page commenters rarely scroll past the
fold.

### Version B — leads with the why (for a more philosophical thread)

```
Every time I leave Claude Code unattended on a long task I have the
same thought: what is actually preventing this thing from doing
something stupid? The honest answer is "a regex denylist in the system
prompt that the model itself enforces." That is bad.

So I built a pre-commit firewall: a Python sidecar that every tool
call posts to before it runs. It returns ALLOW / BLOCK /
REQUIRE_APPROVAL with a signed reason, keeps a tamper-evident audit
chain so I can answer "what did the agent actually try?" three months
later, and exposes a dashboard where I can watch verdicts in real time.

The architecture lifts every tool call into a fixed 2,080-D vector
with 30 named subfields (header, agent state, plan, tool args, safety
flags, memory fingerprint, cost efficiency, hardware band). That
shape unlocks structured per-subfield attribution from the sLLM judge
("blocked 60% on tool_call, 30% on prompt_injection") and statistical
drift detection per subfield via a 5-layer Burn-in baseline.

It implements the T2 (software-only) tier of a 40-claim provisional
patent. T3 hardware claims (CSD, FPGA, TEE attestation) have schema
placeholders ready; the substitution is mechanical because the
external contract stays identical.

326 tests pass, mypy strict over 61 source files, runs in one Docker
container. Demo GIF + dashboard screenshots in the repo.

I'm posting this looking for: prior art I should cite, threat models
I missed, and patches. Code is at <repo URL>.
```

**Char count: ~1,500.**

---

## Comment-thread prep

HN top-comment patterns to anticipate. Drafted replies (post these
yourself, ideally within the first hour while the post is hot):

### "How is this different from OPA / Gatekeeper / [existing policy engine]?"

> OPA evaluates policies, which is most of step 340 in this firewall.
> What's bolted on top is: (a) a fixed-shape vector representation of
> the call so the sLLM judge can return structured per-subfield
> attribution, (b) Ed25519 + Merkle chaining of every verdict so you
> can independently re-verify the audit log, (c) AES-GCM forensic
> journal so tampering surfaces at decrypt time, (d) per-AID circuit
> breaker that auto-quarantines an agent after N violations, (e) ATMU
> (Agent Telemetry Management Unit) 2-phase commit so the firewall
> record and the tool's actual outcome
> are bound. OPA does (a)-step (and well). The other five layers are
> the patent's contribution.

### "Why a sidecar and not a library?"

> Library means in-process and trust-the-caller. Sidecar means the
> firewall has its own signing key, its own audit DB, and its own
> attack surface — the agent process can be compromised without the
> firewall being compromised. Same reason envoy is a sidecar and not
> a library.

### "Why Ed25519 and not [post-quantum]?"

> Ed25519 today, with a Claim-25 ML-DSA stub waiting for when NIST's
> post-quantum slot stabilizes. The codebase has the layering for
> dual-signing; it's just not enabled by default because PyNaCl /
> cryptography don't have ML-DSA upstream yet.

### "Show me the Haiku prompt."

> src/aegis/judge/haiku.py, lines 30-something. SYSTEM_PROMPT requires
> a JSON object with verdict + confidence + attribution (a
> dict[subfield_name, contribution_score] over the 30 named
> subfields). max_tokens=400 to fit. Falls back to the dummy judge
> with rule-based verdicts if ANTHROPIC_API_KEY is absent.

### "326 tests, but how's the coverage?"

> ~85% on aegis.* per pytest --cov. The gaps are mostly in the
> hardware-band placeholders (200-D zero-fill in T2) and the dummy
> providers, which are tested by being substituted into integration
> tests rather than directly.

### "Have you actually used this against a real workload?"

> Yes — it's installed as a Claude Code PreToolUse hook on my Mac
> mini and has been firewalling my own dev work for ~3 weeks. The
> hook is in tools/aegis_hook.py, install procedure in
> tools/README.md. Anecdotally it has caught two real things: a
> "rm -rf node_modules" that was meant to be "rm -rf
> ./packages/foo/node_modules", and an accidentally-committed
> AWS access key that I tried to grep for via `cat ~/.aws/credentials`.

### "Won't this slow down my agent?"

> /evaluate p50 is ~12ms with the dummy providers, ~180ms with
> Haiku judge in the policy-deferred path (which fires only when
> the regex/blast/cost gates haven't already decided). Most calls
> hit one of the cheap fast-path stages. The audit append is
> synchronous but in WAL mode, so contention is bounded.

### "Why a patent?"

> The patent isn't mine — I read it (link in repo) and built one
> possible T2 software reduction. The IP is its author's; the code
> is MIT-licensed [or whichever license you actually use]. Worth
> reading the patent itself; it's well-written.

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

## What to do after submitting

1. **Don't ask anyone to upvote.** HN detects this and will
   silently rank your post lower or kill it outright. Tweet the
   URL once, plainly, with no "please upvote" language.
2. **Respond to comments thoughtfully.** Even hostile ones. The
   comment thread is where Show HN posts win or lose visibility.
3. **Pin the post to your GitHub README** (the LAUNCH.md badge
   pattern below).
4. **Save the comment archive** for your own records — HN comments
   are gold for understanding how technical readers reason about
   your project.

---

## Optional GitHub README badge

Add to the very top of `README.md` (above the demo GIF):

```markdown
[![Discussed on Hacker News](https://img.shields.io/badge/HN-discussed-orange)](<HN URL after posting>)
```

You can grab this badge URL from `shields.io/badge` and update once the
post is live.
