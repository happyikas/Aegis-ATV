# Demo Recording — Media Kit

Pre-rendered artifacts from a single end-to-end run of the AegisData T2
surface. Drop these into a slide deck, blog post, README badge, or PR
description.

All artifacts were generated against a freshly-booted Docker container
(`docker compose up -d --build` from a clean `data/` and `keys/`).
The terminal session was captured with `asciinema`, rendered to GIF
with `agg`, and the dashboard screenshots taken with headless Chrome
at 1440×3200.

```
demo/recording/
├── quickstart.gif            # ★ Personal MVP first-screen GIF (Pillow-generated, 131 KB, 32s loop)
├── draw_quickstart_gif.py    # source: synthetic 6-frame terminal session
├── demo.cast                 # asciinema v3 recording — full Sidecar demo (15 KB)
├── demo.gif                  # rendered Sidecar playback (884 KB, ~25s loop)
├── transcript.log            # plain-text transcript (with ANSI colors)
├── capture_screens.sh        # script that produced the screens
├── narration-60s.txt         # 60-second voiceover script (source)
├── narration-60s.m4a         # 60-second voiceover (Samantha @ 165 wpm, 184 KB)
├── narration-90s.txt         # 90-second voiceover script (source)
├── narration-90s.m4a         # 90-second voiceover (Samantha @ 165 wpm, 372 KB)
└── screens/
    ├── 01-dashboard-overview.png       Full dashboard, idle state
    ├── 01b-dashboard-with-state.png    Dashboard with active quarantine + populated HAM (★ recommended hero shot)
    ├── 02-theater.png                  ATV Theater — single-call breakdown view
    ├── 03-attestation-json.png         Raw /attestation JSON
    ├── 04-replay-json.png              Raw /forensic/replay JSON
    ├── 05-ham-stats.png                Raw /ham/stats JSON
    ├── 06-burnin-status.png            Raw /burnin-status JSON
    ├── 07-admin-aid.png                Raw /admin/aid JSON
    └── 08-openapi-docs.png             Auto-generated OpenAPI / Swagger UI
```

For overlaying the voiceover onto the GIF as a single MP4 file, see
[`docs/RECORDING_KIT.md` § Option B](../../docs/RECORDING_KIT.md).

---

## What each artifact is for

### `demo.gif` (884 KB)

The hero asset. ~25-second loop showing:

1. Title card
2. `docker compose up` + `/healthz` JSON
3. Single `/evaluate` POST → full step-trace verdict
4. `/audit/{aid}` Merkle chain head
5. `python -m demo.agent_demo` running the 5-call scenario + M14 quarantine + M16 HAM
6. `/forensic/replay` decryption tally
7. `/admin/aid` quarantine count + `/cost-attestation/by-tenant` sample record
8. Closing card

Drop into a README at the top:

```markdown
![demo](demo/recording/demo.gif)
```

### `demo.cast` (15 KB)

The asciinema source. Re-renderable to GIF/SVG/MP4 with different
themes/sizes:

```bash
# Re-render with a different theme
agg --theme solarized-dark demo.cast demo-dark.gif

# Generate SVG (vector — clean at any zoom)
asciinema cat demo.cast | svg-term --out demo.svg

# Just play it back in the terminal
asciinema play demo.cast
```

### `transcript.log` (10 KB)

Plain text version of the GIF for copy-pasting into bug reports, GitHub
issues, or PR descriptions. Includes ANSI color codes — render with
`cat` (preserves colors) or `cat -v` (shows escape sequences).

### `screens/01b-dashboard-with-state.png` (★ recommended hero shot)

Full-page screenshot of `localhost:8000` after the demo has populated
state. Shows:

* **Action Firewall pipeline** — all 5 visible stages with the
  rightmost showing "policy + sLLM"
* **ATV-2080-v1 bands** — the color-coded 30-subfield strip
* **Burn-in baseline** — 6 layer slots populated, all in observation
  phase (sample counts visible)
* **Burn-in attestation** — ✓ verified (browser Ed25519) badge lit
* **AID circuit breaker** — red "1 quarantined" badge with
  `recording-quarantined-agent` row showing 3 violations
* **Forensic replay tiles** — 24 decrypted, 0 tampered, 6 aids
* **Hierarchical Agent Memory** — three-column store / recall /
  summarize+ground UI

This single frame proves every M8–M16 surface is live.

### `screens/02-theater.png`

The companion `/theater` page — focuses on a **single ATV** broken
down per-band, with a phase-by-phase narrative ("BEFORE FIREWALL", "AT
EACH FIREWALL CHECK", "VERDICT", "IF PROCEEDS"). Better for a "what is
an ATV?" explainer slide than the dashboard, which is more
operational.

### `screens/03..07-*.png`

Raw JSON responses from the various M11–M16 endpoints. Useful for:

* Showing the actual structured payloads in API documentation
* "This is what your monitoring system would scrape" slides
* Proving that every endpoint returns real, signed, structured data

### `screens/08-openapi-docs.png`

The auto-generated Swagger UI at `/docs`. Useful for:

* Onboarding slides ("here's the API surface")
* Convincing API-conscious reviewers ("there's a real OpenAPI spec
  behind this, not just hand-waving")

---

## Reproducing from scratch

If you change the surface and want to regenerate:

```bash
# 1. Reset state
docker compose down
rm -rf data/* keys/journal_data.key keys/ham_data.key

# 2. Boot fresh
docker compose up -d --build
until curl -sf localhost:8000/healthz; do sleep 1; done

# 3. Record terminal session
TERM=xterm-256color asciinema rec demo/recording/demo.cast \
  --title "AegisData T2 — full surface demo" \
  --idle-time-limit 1.5 \
  --overwrite \
  --command "bash demo/record.sh"

# 4. Render to GIF
agg --theme monokai --cols 100 --rows 30 --font-size 14 --speed 2.0 \
  demo/recording/demo.cast demo/recording/demo.gif

# 5. Capture browser screenshots (the dashboard now has populated state
#    from step 3's demo run)
bash demo/recording/capture_screens.sh

# 6. (Optional) Re-create the active-quarantine hero shot
#    See "Hero shot prep" below

# 7. Tear down
docker compose down
```

### Hero shot prep — recreate `01b-dashboard-with-state.png`

The default `01-dashboard-overview.png` is taken right after the demo
finishes — at which point the M14 quarantine has already been
auto-released. To get the hero shot you need to re-quarantine an AID
and *not* release it:

```bash
# Three disallowed write_file calls under the read-only-role policy
# trip the breaker (max_violations=3 in policies/aid_region.json).
for i in 1 2 3; do
  curl -s localhost:8000/evaluate -H 'content-type:application/json' -d '{
    "header":{"trace_id":"qt'$i'","span_id":"qs'$i'","tenant_id":"demo-tenant",
              "aid":"recording-quarantined-agent","ats":"ATV-2080-v1",
              "schema_version":"ATV-2080-v1","tier_profile":"T2",
              "cost_attestation_profile":"software","timestamp_ns":'$(date +%s%N)'},
    "agent_state_text":"trying disallowed tool",
    "plan_text":"unauthorized write attempt",
    "tool_name":"write_file","tool_args_json":"{\"path\":\"./evil.txt\"}",
    "role_id":"read-only-role",
    "safety_flags":{},
    "cost_estimate":{"input_token_count":50,"cumulative_dollars":0.0001,
                     "forecasted_cost_to_completion":0.01}
  }' >/dev/null
done

# Verify
curl -s localhost:8000/admin/aid | jq

# Capture
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1440,3200 --virtual-time-budget=4000 \
  --screenshot=demo/recording/screens/01b-dashboard-with-state.png \
  http://localhost:8000

# Clean up — release the quarantine for the next recording
curl -s -X POST localhost:8000/admin/aid/release \
  -H "X-Aegis-Admin-Token: dev-admin-token" \
  -H 'content-type: application/json' \
  -d '{"aid":"recording-quarantined-agent","reason":"hero shot done"}' | jq
```

---

## Suggested usage by audience

### Hiring manager / recruiter

1. `screens/01b-dashboard-with-state.png` — single hero shot.
2. `demo.gif` — embedded in the README, autoplays on GitHub.
3. Link to [`docs/QUICKSTART.md`](../../docs/QUICKSTART.md) so they can
   run it themselves in 60 seconds.

### Technical interviewer

1. `demo.gif` first to set context (~25s).
2. `screens/02-theater.png` to walk through what an ATV actually is.
3. `screens/08-openapi-docs.png` to show the API surface.
4. `transcript.log` as the "everything that happened in that GIF, in
   text" reference.

### Patent reviewer / IP counsel

1. `screens/01b-dashboard-with-state.png` to prove every M8–M16 panel
   exists.
2. `screens/03-attestation-json.png` + `04-replay-json.png` +
   `07-admin-aid.png` — raw structured payloads matching the
   patent's claim language.
3. [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) §
   "What stays software in T2 vs. moves to hardware in T3"
   for the substitution boundary.

### Investor / executive

1. `demo.gif` only.
2. README headline: "16 patent-aligned milestones, 326 tests, runs
   in one container."
3. [`README.md`](../../README.md) endpoint table for breadth.

---

## Sharing checklist

Before posting any of these:

- [ ] No `.env` content visible in any frame
- [ ] No private keys (`keys/*.pem`, `keys/*.key`) named or shown
- [ ] No real `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in `printenv`
      output (the demo doesn't need them but a recording shell might
      have them set)
- [ ] No real customer / production data in tool args (the demo uses
      `demo-tenant` and synthetic agent ids only)
- [ ] Filename in URL bar matches the public dashboard route, not
      a privileged admin path with embedded tokens

The artifacts in this directory have been audited against this
checklist and are safe to publish.
