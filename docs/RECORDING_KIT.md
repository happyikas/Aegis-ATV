# Live Recording Kit — Loom / YouTube / OBS

Everything you need to record a narrated video on top of the GIF that
already lives in `demo/recording/demo.gif`. The text is camera-ready
(teleprompter-friendly with breath cues). The OBS scene + browser
setup is dialled to the existing screenshots' aspect ratio.

> **Why a separate doc?** [`docs/DEMO.md`](DEMO.md) is the *what to
> click* playbook — it describes scenes by beats. This doc is the
> *what to say* playbook — it gives you the actual narration in three
> lengths, plus the production setup so the voice and visuals
> actually sync up.

---

## §1 — Three narration scripts

All three are written for **a calm, unhurried delivery at ~140 wpm**.
Pause cues are in `[brackets]`. Em-dashes are read with a slight
pause; commas are read inline.

### 1A — 60-second cut (Loom hover, LinkedIn auto-play, GIF caption)

**Use for:** thumbnails-with-narration, LinkedIn video feed, embedded
README hover. Total spoken: ~140 words.

> [calm] Most AI agent frameworks today have nothing actually stopping
> them from doing something stupid. The guard rails are usually a
> regex denylist that the model itself enforces. [pause]
>
> So I built a real one. [pause]
>
> AegisData T2 is a Python sidecar. Every tool call your agent makes
> goes through a seven-stage firewall before it runs. Argument
> inspection. AID authorization. Blast radius. Cost forecast. Policy
> plus sLLM judge. [breath]
>
> The verdict gets Ed25519-signed, Merkle-chained into a tamper-
> evident audit log, and AES-256-GCM encrypted into a separate
> forensic journal. [pause]
>
> Three hundred and twenty-six tests, mypy strict, runs in one Docker
> container. [pause]
>
> Code in the description. [end]

**Recording note:** record this once, no edits, no jump cuts. If you
fluff a line, restart from the top — 60 seconds is short enough to
re-do cleanly.

---

### 1B — 90-second cut (YouTube short, Twitter video, README inline)

**Use for:** the canonical "what is this thing" video. Total spoken:
~210 words.

> [calm, slightly slower than 1A]
>
> Every time I leave Claude Code or Cursor unattended on a long task,
> I have the same thought: what is actually preventing this thing from
> doing something stupid? [pause] The honest answer in most setups is
> "a regex denylist that the model itself enforces." That's not a
> firewall. That's a sticker. [pause]
>
> So I built a real firewall. [breath]
>
> AegisData T2 is a Python sidecar that sits between your agent and
> its tools. Every call goes through a seven-stage pipeline. Argument
> inspection catches obvious shell injection. AID authorization checks
> if this agent is even allowed to use this tool. Blast radius
> escalates dangerous operations to human approval. Cost forecast
> gates anything over your budget. The policy engine matches against
> rules, and falls back to a small LLM judge — Claude Haiku in this
> build — that returns a verdict plus per-subfield attribution scores.
> [pause]
>
> Every verdict gets Ed25519-signed and Merkle-chained into a tamper-
> evident audit log. Every record also lands in an AES-256-GCM
> encrypted journal where the cleartext header is used as
> additional-authenticated-data — so flipping any byte fails decrypt.
> [breath]
>
> There's a per-AID circuit breaker that auto-quarantines runaway
> agents. There's a Hierarchical Agent Memory store with bound-AAD
> encryption. And there's a 5-layer Burn-in controller that tracks
> statistical drift per tenant, role, and instance. [pause]
>
> Three hundred and twenty-six tests passing, mypy strict over 61
> source files, runs in one Docker container. [pause]
>
> Repo and full architecture write-up in the description. [end]

---

### 1C — 5-minute deep-dive (technical YouTube upload)

**Use for:** the full technical walkthrough. Total spoken: ~700 words.
Sections marked with `▼ scene N` correspond to the OBS scenes in §2.

> [calm, conversational]
>
> ## ▼ scene 1 — title + problem (0:00–0:30)
>
> Hey. I'm going to walk you through AegisData T2, an action firewall
> for AI agents. [pause]
>
> The problem this solves is simple. If you're running Claude Code, or
> Cursor, or a homegrown LangChain agent unattended, the only thing
> stopping it from doing something destructive is usually a regex
> denylist that the model itself enforces. That's not security. The
> moment the model decides to "interpret your intent generously," your
> guard rails are gone. [pause]
>
> What's missing is a real pre-commit firewall — something every tool
> call posts to before it runs, that returns ALLOW, BLOCK, or REQUIRE
> APPROVAL with a signed reason, and that keeps a tamper-evident
> record so you can answer "what did the agent actually try?" three
> months later. [breath]
>
> ## ▼ scene 2 — single /evaluate (0:30–1:15)
>
> Let me show you what one tool call looks like. I'll send a curl to
> POST slash evaluate with a read file request. [type+enter]
>
> The response is a verdict — ALLOW in this case — plus a step-trace
> object showing exactly what each of the seven firewall stages
> decided. Argument inspection passed. AID authorization passed. Blast
> radius came back as one. Cost forecast was inside the budget. The
> policy engine matched a safe-read rule, so we never even called the
> sLLM judge. [pause]
>
> The whole thing is Ed25519-signed. The signature is at the bottom of
> the response. You can re-verify it with any standard library — the
> public key is exposed at slash attestation. [breath]
>
> ## ▼ scene 3 — audit chain (1:15–1:45)
>
> Now if I hit slash audit slash my-agent, [type+enter] I get the
> full chain of records for that agent. Every record carries a SHA3
> link to its predecessor — that's the chain valid flag the dashboard
> shows in green. The chain is independently re-verifiable; you don't
> have to trust the server. [pause]
>
> ## ▼ scene 4 — the dashboard (1:45–3:00)
>
> Let me show you the dashboard. [switch scene]
>
> Top of the page: the form for crafting a tool call by hand. Below
> that, the seven-stage pipeline visualizes each step's verdict. To
> the right, the ATV-2080-v1 band strip — that's a color-coded view
> of the 2,080-dimension vector that represents this call. Thirty
> named subfields. The patent's appendix A. [pause]
>
> Down here is the audit chain. Five records, all green, all chained.
> [scroll]
>
> Burn-in baseline — five layer slots, each tracking statistical
> maturity per tenant, role, and instance. They graduate observation
> to shadow to assisted to production once they hit the patent's
> threshold gates. [scroll]
>
> Burn-in attestation — this is a measurement of the running code,
> the policies, and the signing key. The dashboard re-verifies the
> Ed25519 signature in your browser — see the green badge. [scroll]
>
> AID circuit breaker. Three unauthorized tool attempts and the agent
> gets auto-quarantined. Future calls hard-blocked at step 315 until
> an admin token releases it. [scroll]
>
> Forensic replay — every audit record is also AES-256-GCM encrypted
> into a separate journal. This button decrypts the whole thing,
> rebuilds the per-AID hash chain, and reports tampered records.
> [scroll]
>
> Hierarchical Agent Memory — six operations: memory, recall,
> context, forget, summarize, ground. Every body is encrypted with
> AAD bound to the tenant, agent, and sequence number. Tamper any of
> those and decrypt fails. [pause]
>
> ## ▼ scene 5 — the demo script (3:00–4:15)
>
> Let me run the canned demo. This is the agent_demo Python module —
> five hand-tuned tool calls hitting every verdict class, plus the
> circuit breaker scenario, plus the HAM exercise. [type+enter]
>
> Watch the read file pass. The write file pass. The shell with rm
> dash rf gets blocked at step 310 — regex caught it. The 5-gigabyte
> write triggers approval because the cost forecast exceeded the
> budget. The transfer funds triggers approval because the blast
> radius is too high. [pause]
>
> Now the M14 scenario — three disallowed write file calls under the
> read-only role. The breaker trips after the third. The next call,
> even a legitimate read, gets hard-blocked. The admin token releases
> the AID. The next read passes. [pause]
>
> M16 — three memory items stored, recalled by tag, bundled into a
> context, grounded against a claim, one tombstoned. [pause]
>
> M15 — forensic replay across everything we just did. Twenty
> records decrypted, zero tampered, four AIDs, four out of four
> chains valid. [breath]
>
> ## ▼ scene 6 — close (4:15–5:00)
>
> So that's AegisData T2. Sixteen patent-aligned milestones. Three
> hundred and twenty-six tests passing. Mypy strict over sixty-one
> source files. Runs in one Docker container. [pause]
>
> The repo is in the description. The 60-second install path is
> docs/QUICKSTART.md. The per-milestone source tour is
> docs/ARCHITECTURE.md. The production runbook — env vars, key
> rotation, AID admin, journal forensics, backup/restore — is
> docs/OPERATIONS.md. [pause]
>
> Thanks for watching. Comments and patches welcome. [end]

---

## §2 — OBS scene setup

**Output**: 1080p, 30 fps, 5 Mbit/s constant. YouTube and Loom both
re-encode anyway, so don't burn bandwidth.

### Six scenes

| # | Name | Layout | Duration in 5-min cut |
|---|---|---|---|
| 1 | Title | Static title card (PNG export) + your webcam bottom-right | 0:00–0:10 |
| 2 | Terminal-only | Full-screen terminal at iTerm2 1440×900 | 0:30–1:45 |
| 3 | Browser-only | Full-screen browser at 1440×3200 (use scene transitions to scroll) | 1:45–3:00 |
| 4 | Split | Left: terminal (50%), Right: browser (50%) | 3:00–4:15 |
| 5 | Webcam-only | Full-screen webcam | (cuts to camera for closing 4:15–5:00) |
| 6 | End card | Static PNG with repo URL + your handle | 5:00–end |

### Title card spec

`demo/recording/screens/title-card.png` (you'll need to make this —
suggested layout):

* **1920×1080**
* Background: deep navy `#0f172a`
* Centered: `AegisData T2`
* Subtitle: `Action Firewall for AI Agents · 16 milestones · 326 tests`
* Bottom: `<your handle>` · GitHub repo URL

### End card spec

Same canvas, different text:

* `Try it →`
* `docker compose up -d`
* `localhost:8000`
* GitHub URL

### Terminal setup (iTerm2 or Alacritty)

* **Font**: JetBrains Mono 18pt or Berkeley Mono 17pt. Big.
* **Theme**: light background — recordings on white show better in
  thumbnails than dark mode. Recommended: Solarized Light or GitHub.
* **Window**: 1440×900, no title bar (Cmd-Shift-F).
* **Prompt**: minimal. `PS1='$ '`. The dollar sign is the visual
  anchor; no usernames, no paths, no git status — they're noise.
* **Clear scrollback** before each take: `clear && printf "\e[3J"`.

### Browser setup (Chrome or Arc)

* **Window**: 1440×900, hide bookmarks bar, hide all extensions.
* **URL bar**: just `localhost:8000` — no `127.0.0.1:8000`, no
  `:8000/path`.
* **Zoom**: 110% — the dashboard's defaults are dense; bumping to
  110% makes the type read at 1080p.
* **Colour scheme**: Light. The dashboard is designed for light mode.
* **DevTools**: closed. Open them only if you need to demo a
  WebCrypto verification, then close again.

### Webcam

* **Resolution**: at least 1080p. The Mac mini's iSight or any
  decent USB cam.
* **Lighting**: one diffused light, slightly above eye level, on the
  same side as your dominant hand.
* **Background**: solid wall or bookshelf. No moving objects.
* **Audio**: USB condenser mic (Blue Yeti or similar) at fist's-
  distance from your mouth, slightly off-axis to avoid plosives.
  **Don't** use AirPods.

---

## §3 — Recording day checklist

### 30 minutes before

- [ ] Restart your machine. Don't record on a 12-day-uptime laptop —
      caches are full and stuff is slow.
- [ ] `docker compose down && rm -rf data/* keys/journal_data.key keys/ham_data.key`
- [ ] Close Slack, Mail, Calendar, anything that pops notifications
- [ ] System Settings → Notifications → Do Not Disturb → on
- [ ] Plug in power. Battery throttling shows up on screen recordings.
- [ ] Hide your desktop icons. macOS: System Settings → Desktop &
      Stage Manager → Show items: off

### 5 minutes before

- [ ] `docker compose up -d --build`
- [ ] `until curl -sf localhost:8000/healthz; do sleep 1; done`
- [ ] Open browser at `localhost:8000` — verify the green "service
      healthy" dot
- [ ] Open terminal in the repo root — verify `pwd` shows
      `…/MVP` and nothing weird
- [ ] `printenv | grep -i key` — make sure no API keys are in the
      env that the camera will see. If there are, open a new shell
      tab without them sourced.
- [ ] Mic check — record 10 seconds, listen back. Adjust gain so
      your normal speaking voice peaks at -12 dB.

### During the take

- [ ] Speak slower than feels natural. The 60s/90s scripts are
      timed for ~140 wpm; most people read at 180+ wpm cold.
- [ ] **Don't** read the script verbatim. Glance at it once per
      sentence; speak the idea. Reading is audible.
- [ ] If you fumble a sentence, stop, count to three (so the audio
      cut is clean), then restart the sentence. Don't restart the
      whole take.
- [ ] Click only when the text describes the click. Premature
      clicks make the viewer's eye chase.

### After the take

- [ ] Listen to the first 60 seconds with headphones. Plosives,
      sibilance, room echo — fix in post if minor, re-record if
      major.
- [ ] Watch the full take at 2× speed. If you find yourself
      surprised by anything visible (URL bar, file path, env), it's
      a re-record.
- [ ] **Save the raw OBS recording** (lossless) before you do any
      editing. You will want it later.

---

## §4 — Post-production minimums

### Edits

The 60s and 90s cuts should need **zero edits**. Record clean takes.

The 5-minute cut may need:

* A 1-second fade-in at the title card and fade-out at the end card.
* Audio normalization to -16 LUFS (YouTube spec).
* Maybe a silent gap trimmed if you paused too long between scenes.

That's it. Don't add background music, don't add zoom-bounces, don't
add stock-footage cutaways. The dashboard is the visual hook;
embellishment makes it look like a marketing video.

### Caption files

YouTube auto-captions are decent but make mistakes on technical
terms. Provide a manual `.srt` for any video over 2 minutes.

The 90s narration script in §1B is already paragraph-aligned to
roughly 8-second segments. Convert with:

```bash
# Crude but works: assume one segment per paragraph, 8s each
awk 'BEGIN{i=1} /^>/{seg=substr($0,2); printf "%d\n00:00:%02d,000 --> 00:00:%02d,000\n%s\n\n", i, (i-1)*8, i*8, seg; i++}' \
  docs/RECORDING_KIT.md > demo/recording/captions-90s.srt
```

(Or spend 10 minutes doing it by hand — better timing.)

---

## §5 — Upload metadata

### YouTube

**Title** (≤ 100 chars):

> AegisData T2 — I built an action firewall for AI agents | Demo + architecture walkthrough

**Description** (first 2 lines show in search; rest is collapsible):

> A Python sidecar that wraps every AI agent tool call in a 2,080-D
> vector, runs it through a 7-stage firewall, signs the verdict with
> Ed25519, and chains it into a tamper-evident audit log. Demo + full
> architecture walkthrough.
>
> Code: <repo URL>
>
> Chapters:
>   0:00 The problem
>   0:30 Single /evaluate POST
>   1:15 The audit chain
>   1:45 Dashboard tour
>   3:00 Full demo (M14 quarantine + M15 replay + M16 HAM)
>   4:15 Closing
>
> Docs:
>   60-second install:    docs/QUICKSTART.md
>   Surface tour:         docs/ARCHITECTURE.md
>   Production runbook:   docs/OPERATIONS.md
>   Patent-aligned plan:  PLAN_v2.md
>
> Built with: Python 3.11, FastAPI, SQLite WAL, Ed25519, SHA3-256
> Merkle, AES-256-GCM, Claude Haiku 4.5, OpenAI embeddings, uv,
> pytest, ruff, mypy strict, Docker.
>
> Patent: AegisData provisional v7.10 — implements the T2 (software-
> only) tier; T3 hardware claims (CSD, FPGA, TEE attestation) have
> schema placeholders.
>
> ----
>
> Tags: AI safety, agent firewall, LLM guard rails, Ed25519,
> tamper-evident audit, Claude Code, Cursor, LangChain, software
> security, distributed systems, Python, FastAPI

**Tags** (YouTube's tag field, ≤ 500 chars total):

> AI safety, AI agents, agent firewall, LLM, Claude Code, Cursor,
> LangChain, Ed25519, audit log, tamper-evident, software security,
> Python, FastAPI, Docker, sidecar, distributed systems, patent,
> AegisData, action firewall, AEAD, AES-GCM

**Thumbnail**: `demo/recording/screens/01b-dashboard-with-state.png`,
cropped to 1280×720 with the AID circuit breaker panel + replay
tiles centered. Add a 60pt-bold overlay text in the top-left:
"I built a firewall for AI agents." High contrast, no drop shadow.

### Loom

Loom titles auto-generate from the first thing you say. So **say the
title clearly in the first 3 seconds of the recording**:

> "AegisData T2 — action firewall for AI agents."

That becomes the Loom title. The rest of the metadata is filled in
from your YouTube description.

### LinkedIn

Upload the 90-second cut natively (don't link to YouTube — LinkedIn's
algorithm penalizes outbound video links). First-line caption:

> Most AI agent frameworks have nothing actually stopping them from
> doing something stupid. Here's what a real firewall looks like ↓

Second line: link to repo. Stop there. LinkedIn's "see more" cuts at
~210 chars; everything past that is invisible to scrollers.

---

## §6 — Three pre-recorded artifact options

If you don't want to record at all, these three options give you a
"video" without a camera or a microphone:

### Option A — Use the existing GIF

Already done. `demo/recording/demo.gif` is a 25-second screencast of
the terminal flow. Embed in any blog post or LinkedIn post directly;
it's 884 KB and autoplays.

### Option B — Pre-rendered macOS `say` narration (already in the repo)

Two AAC voiceover tracks live alongside the GIF, generated with macOS
`say` and the Samantha voice at `-r 165`:

| File | Duration | Size |
|---|---|---|
| `demo/recording/narration-60s.m4a` | 43.9s | 184 KB |
| `demo/recording/narration-90s.m4a` | 90.4s | 372 KB |

The source text for each is sibling: `narration-60s.txt`,
`narration-90s.txt`. Re-render at a different speed/voice with:

```bash
say -v 'Samantha' -r 140 \
    -o demo/recording/narration-90s.aiff \
    -f demo/recording/narration-90s.txt
afconvert -f m4af -d aac \
    demo/recording/narration-90s.aiff \
    demo/recording/narration-90s.m4a
```

Lower `-r` = slower delivery. 140 wpm is unhurried; 165 is the rate
the bundled tracks use; 200+ starts to feel rushed.

**Premium voices** ("Samantha (Premium)", "Ava (Premium)", etc.) are
noticeably more natural but require a one-time download via
System Settings → Accessibility → Spoken Content → System Voice →
Manage Voices. Once installed, swap the `-v` value above.

To overlay on the existing GIF, convert the GIF + audio to MP4 with
ffmpeg (`brew install ffmpeg`):

```bash
ffmpeg -stream_loop -1 -i demo/recording/demo.gif \
       -i demo/recording/narration-90s.m4a \
       -shortest -c:v libx264 -pix_fmt yuv420p \
       -c:a aac -b:a 128k \
       demo/recording/demo-with-narration.mp4
```

(`-stream_loop -1` loops the 25s GIF until the 90s audio ends;
`-shortest` cuts at audio end.)

This is a fallback. A real human voice is always better.

### Option C — Asciinema-only "video"

The `demo.cast` file is, literally, a video — replayable in any
browser via the asciinema-player JS embed:

```html
<script src="https://asciinema.org/a/embed.js" async></script>
<asciinema-player src="/path/to/demo.cast" cols="100" rows="30">
</asciinema-player>
```

Works on a personal blog without uploading to YouTube. Searchable
(asciinema preserves the text), copy-pasteable, and 60× smaller than
the equivalent video.

---

## §7 — Distribution sequence

Once you have the 90-second video:

1. **Day 0**: Upload to YouTube (unlisted), Loom (private), and add
   to repo as `demo/recording/demo-90s.mp4` (if file size OK; gitignore
   if > 5 MB).
2. **Day 0**: Embed in `LAUNCH.md` (already linked).
3. **Day 1, morning**: Post to Show HN (see [`SHOW_HN.md`](../SHOW_HN.md)).
   Don't link the video; the GIF in the README is enough. The video
   is for Twitter/LinkedIn.
4. **Day 2, morning**: X/Twitter thread (see
   [`TWITTER_THREAD.md`](../TWITTER_THREAD.md)). Attach the 90-second
   video to the first post.
5. **Day 2, afternoon**: LinkedIn post. Native video upload.
6. **Day 3+**: Make the YouTube video public. Send the link to anyone
   who asked questions in the HN/Twitter threads.

The 5-minute cut goes up on YouTube whenever it's ready —
no urgency. It's the long-tail asset for someone who clicked through
from the 90-second cut and wants more.
