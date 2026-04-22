# X/Twitter announcement thread

> X's hard limit is 280 chars per post. Threads of 8–12 posts perform
> best. The first post needs to stand alone — most people see only
> that. Attach the demo GIF (884 KB, well under X's 15 MB limit) to
> the first post.

Three thread variants below. Pick one, or shuffle posts between them.

---

## Thread A — leads with the demo (recommended)

Best for: maximum click-through. Visual first, technical second.

### Post 1 — hook + GIF

> i built an action firewall for AI agents.
>
> every tool call your agent makes goes through a 7-stage pipeline,
> gets a verdict + Ed25519 signature, lands in a tamper-evident
> Merkle audit chain, and gets AES-GCM-encrypted into a forensic
> journal.
>
> 326 tests. one Docker container. ↓

[attach demo/recording/demo.gif]

### Post 2 — the architecture

> the architecture lifts every tool call into a fixed 2080-D vector
> with 30 named subfields:
>
> header · agent_state · plan · tool_call · safety_flags ·
> memory_fp · cost_efficiency · hardware_band
>
> patent's idea — works really well in practice.

### Post 3 — why fixed-shape

> the fixed shape is the whole game. the sLLM judge can return
> per-subfield attribution scores ("blocked 60% on tool_call, 30% on
> prompt_injection, 10% on cost_efficiency.forecasted_cost").
>
> structured explanations as first-class output, not hopeful strings.

### Post 4 — Burn-in

> there's a 5-layer × 4-phase Burn-in controller tracking statistical
> baselines per tenant / role / agent.
>
> each layer slot graduates observation → shadow → assisted →
> production once it has 1000 labelled samples + TPR≥0.95 / FPR≤0.02
> / precision≥0.90.

### Post 5 — circuit breaker

> per-AID circuit breaker. three unauthorized tool attempts → the
> agent gets auto-quarantined → future calls hard-blocked at step
> 315 → admin token to release.
>
> patent §5B. T2 software emulation; T3 puts it in the hardware tag
> comparator.

### Post 6 — encrypted journal

> every audit record is also AES-256-GCM encrypted into a forensic
> journal. the cleartext header (tenant, aid, atv_commitment, ts) is
> used as additional-authenticated-data.
>
> flip a bit anywhere and decrypt fails. tamper-evidence at decrypt
> time, not display time.

### Post 7 — HAM

> Hierarchical Agent Memory — the patent's 4-level memory hierarchy,
> T2-emulated as L3+L4 with encrypted SQLite + in-process LRU.
>
> 6 ops: memory / recall / context / forget / summarize / ground.
> bodies AES-GCM with AAD bound to (tenant_id, aid, seq).

### Post 8 — what surprised me

> what surprised me building it:
>
> 1. fixed-shape vectors are underrated
> 2. the patent is full of working software, not handwaving
> 3. hardest bug was a re-entrant lock in the circuit breaker
> 4. AEAD is a beautiful primitive
>
> writeup: <link to LAUNCH.md>

### Post 9 — try it

> 60-second install:
>
>   docker compose up -d
>   curl -s localhost:8000/healthz
>   open localhost:8000
>
> code: <repo URL>
> per-milestone source tour: docs/ARCHITECTURE.md
> dashboard hero shot: ↓

[attach demo/recording/screens/01b-dashboard-with-state.png]

### Post 10 — close

> implements the T2 software tier of a 40-claim provisional patent.
> T3 hardware claims (CSD, FPGA, TEE) have schema placeholders ready;
> the substitution is mechanical because the external contract stays
> identical.
>
> comments / criticism / prior-art pointers all welcome.

---

## Thread B — leads with the why (more philosophical)

Best for: an audience that already knows the AI-safety landscape and
wants to hear what's missing in current approaches.

### Post 1 — the problem

> if you've ever left Claude Code or Cursor running unattended on a
> long task, you've had this thought:
>
> what is actually preventing this thing from doing something stupid?
>
> the honest answer is "a regex denylist that the model itself
> enforces." that is bad.

### Post 2 — what's missing

> what's missing is a pre-commit firewall — something every tool call
> posts to before it runs, that returns ALLOW / BLOCK /
> REQUIRE_APPROVAL with a signed reason, and that keeps a tamper-
> evident record so you can answer "what did the agent actually try?"
>
> three months from now.

### Post 3 — what I built

> so i built one.
>
> AegisData T2 — Python sidecar, FastAPI, runs in one Docker
> container, 326 tests passing.
>
> [demo GIF attached]

[attach demo/recording/demo.gif]

[continue with posts 2–10 from Thread A]

---

## Thread C — leads with a single technical claim

Best for: developer-tooling Twitter. Hooks the architecture-curious.

### Post 1 — the claim

> hot take: every AI tool call should be a fixed-shape vector before
> it gets a verdict.
>
> i built one and the side effects are wild:
>
> - structured per-subfield attribution from the judge model
> - statistical drift detection per subfield via Burn-in baselines
> - tamper detection because the vector is part of the signed payload

### Post 2 — the shape

> 2080 dimensions, 30 named subfields, allocated:
>
> header (64) · agent_state (512) · plan (512) ·
> tool_call (384) · safety_flags (256) · memory_fp (136) ·
> cost_efficiency (16) · hardware_band (200, T2-zeroed)
>
> not arbitrary — implements a 40-claim patent's appendix A.

[continue with posts 4–10 from Thread A]

---

## Visual assets to attach

X compresses everything to ~5 MB JPEG/PNG / ~512 MB MP4. The GIF
(884 KB) is fine. The dashboard screenshot is ~385 KB.

| Post | Asset | Path |
|---|---|---|
| 1 | Demo GIF | `demo/recording/demo.gif` |
| 9 | Dashboard hero shot | `demo/recording/screens/01b-dashboard-with-state.png` |
| (optional) | Theater view | `demo/recording/screens/02-theater.png` |
| (optional) | OpenAPI docs | `demo/recording/screens/08-openapi-docs.png` |

For the dashboard hero shot, **crop to 1440×1600** (top half only) so
the AID circuit breaker, replay tiles, and burn-in attestation are
all visible above the fold of X's preview.

---

## Hashtags + handles

X's algorithm doesn't reward hashtag spam; pick 1–2 max for the first
post:

* `#AIagents` — broad reach
* `#aisafety` — narrower, more thoughtful audience
* `#opensource` — only if your repo is actually open-licensed

People worth tagging if they're likely to engage:

* `@simonw` — built llm CLI, posts often about agent safety
* `@karpathy` — broad AI audience, occasionally engages with safety
* `@AnthropicAI` / `@AnthropicSafety` — Claude is the judge model;
  worth a polite mention but don't @-spam
* `@CursorComposer` / `@cursor_ai` — Cursor users are a target
  audience for this
* `@LangChainAI` — same; their audience is "agents that need guard
  rails"

**Don't tag** unless the thread is genuinely good. A weak thread with
big tags is worse than a strong thread with no tags.

---

## Timing

* **Best**: Tuesday or Wednesday, 9–11am US Pacific. That catches
  the SF developer scroll.
* **Acceptable**: Thursday afternoon for European audience.
* **Avoid**: Friday evening, weekends, Monday morning. Twitter dies
  Friday at 5pm and doesn't wake up till Tuesday.

If you also do the Show HN post, **stagger them by 24h** — same
audience overlap but different reading modes. HN first (deeper read
crowd), Twitter the next morning (broader skim).

---

## What to NOT do

- **No `🚀 Excited to announce 🎉`.** It reads as marketing, not
  building.
- **No "thread 🧵" emoji as post 1.** Lead with substance; the thread
  arrow is implicit when you reply to yourself.
- **No "let me know what you think!"** as a closer. Specific asks
  outperform — "looking for prior-art pointers" / "patches welcome"
  / "what threat models did I miss".
- **No screenshots of the codebase as JPEGs.** If you want to show
  code, use a syntax-highlighted screenshot from carbon.now.sh, or
  link to the actual file on GitHub.

---

## Reply-thread playbook

The first 60 minutes after the first post is when most engagement
happens. Have these ready to drop in as quote-tweets or replies:

* **If someone asks how it compares to OPA**: paste the comment from
  `SHOW_HN.md` § "How is this different from OPA…"
* **If someone asks why a sidecar**: paste the "Why a sidecar" comment
* **If someone asks for the Haiku prompt**: link directly to
  `src/aegis/judge/haiku.py` line range
* **If someone says "this won't scale"**: latency table from the
  README — p50 12ms dummy / 180ms judge-deferred path

Don't ad-lib these answers — too easy to fumble in the moment.
