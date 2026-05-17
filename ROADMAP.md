# Aegis ATV — Roadmap

> Public roadmap kept in this file *and* mirrored to a GitHub Project
> board (the board is the canonical view for triage; this file is the
> stable URL for outsiders). Last refreshed: 2026-05-16.

For the most-recent issue activity see
[the issues list](https://github.com/happyikas/Aegis-ATV/issues).

---

## Now — in flight

_(no work currently in flight — MVP surface is closed; see "Next" for
external-event-gated items.)_

---

## Next — external-event-gated

The remaining MVP items all wait on something outside the repo. Code
work is done; gating is design-partner availability or upstream
platform readiness.

| Priority | Issue | One-line | Blocker |
|---|---|---|---|
| 🟡 mid | [#151 — Show HN go-live](https://github.com/happyikas/Aegis-ATV/issues/151) | submission to Hacker News | design-partner reference (1+) we can link to from the post body |
| 🔴 low | [#147 — Gap D](https://github.com/happyikas/Aegis-ATV/issues/147) | inter-agent edge tracking (`inter_agent_edges`) | OpenClaw runtime cooperation (upstream must emit `parentAgentId` / `parentInvocationId`) |
| 🔴 low | [#150 — ClawHub marketplace](https://github.com/happyikas/Aegis-ATV/issues/150) | plugin discovery via ClawHub | ClawHub marketplace not yet public |

---

## Done — recent

(For the full release log see [`CHANGELOG.md`](CHANGELOG.md). Below
is grouped by release theme — 47 PRs landed across v0.4.0 → v0.6.0
between 2026-05-12 and 2026-05-16, so a flat row-per-PR table would
be unreadable. PR numbers are the squash-merge commits on `main`.)

### v0.6.0 — semantic search (2026-05-16)

| PR | Title |
|---|---|
| [#216](https://github.com/happyikas/Aegis-ATV/pull/216) | Embedding-based ranker over the wiki — `aegis knowledge search --engine {tfidf,embedding}`. Reuses the existing `aegis.atv.embeddings` provider chain (bge-local / openai / dummy); paraphrases now rank without lexical overlap |

### v0.5.20 – v0.5.27 — autonomy & knowledge maturation (2026-05-13 → 2026-05-17)

| PR | Title |
|---|---|
| [#215](https://github.com/happyikas/Aegis-ATV/pull/215) | `aegis autonomy explain <trace_id>` — 11-gate forensic walker (original audit stamps + current-state replay) |
| [#214](https://github.com/happyikas/Aegis-ATV/pull/214) | Docs sync — CLAUDE.md + README catch-up across v0.5.20–0.5.25 |
| [#213](https://github.com/happyikas/Aegis-ATV/pull/213) | Session-prior calibration — `exploring`/`refactor`/`prod-deploy` risk labels scale `min_trust` (8h TTL) |
| [#212](https://github.com/happyikas/Aegis-ATV/pull/212) | Andon tripwire — consecutive-bypass cap (default 20) forces human on N+1 |
| [#211](https://github.com/happyikas/Aegis-ATV/pull/211) | ATV centroid bypass — per-pattern Mahalanobis-diagonal gate in 3-D log-feature space; >3σ refuses bypass |
| [#210](https://github.com/happyikas/Aegis-ATV/pull/210) | Reversibility scoring — 23-rule policy; irreversible actions never auto-bypass (hard safety floor) |
| [#209](https://github.com/happyikas/Aegis-ATV/pull/209) | TF-IDF semantic search over wiki — `aegis knowledge search <query>` (pure-Python, deterministic) |
| [#208](https://github.com/happyikas/Aegis-ATV/pull/208) | Wiki SESSION / INCIDENT / WORKFLOW entry kinds (event-level facets) |

### v0.5.11 – v0.5.19 — autonomy backbone + LLM-wiki layer (2026-05-12 → 2026-05-13)

| PR | Title |
|---|---|
| [#207](https://github.com/happyikas/Aegis-ATV/pull/207) | Docs sync — CLAUDE.md + README catch-up across v0.5.11–0.5.18 |
| [#206](https://github.com/happyikas/Aegis-ATV/pull/206) | Wiki-grounded advisor measurement + e2e demo (`aegis knowledge measure`) |
| [#205](https://github.com/happyikas/Aegis-ATV/pull/205) | Production advisor path plumbed to wiki — local hook passes `aid` through |
| [#204](https://github.com/happyikas/Aegis-ATV/pull/204) | sLLM advisors consume the knowledge wiki (`AEGIS_ADVISOR_USE_KNOWLEDGE=1`) |
| [#203](https://github.com/happyikas/Aegis-ATV/pull/203) | ContextMemory LLM-wiki knowledge layer — `~/.aegis/knowledge/` derived from raw JSONL |
| [#202](https://github.com/happyikas/Aegis-ATV/pull/202) | Explicit-deny CLI (`aegis autonomy deny`) + doctor postmortem section |
| [#201](https://github.com/happyikas/Aegis-ATV/pull/201) | Autonomy runtime wiring — `evaluate.py` + local hook hot-path activated |
| [#200](https://github.com/happyikas/Aegis-ATV/pull/200) | Bayesian backbone — Beta posterior, LCB, decay, JS-drift, ECE-gated calibration |
| [#199](https://github.com/happyikas/Aegis-ATV/pull/199) | Human-in-the-loop minimiser — `aegis autonomy learn/show` + trust table |

### v0.5.7 – v0.5.10 — production gaps + sLLM brains (2026-05-12)

| PR | Title |
|---|---|
| [#198](https://github.com/happyikas/Aegis-ATV/pull/198) | TripleAxisAdvisor — sLLM scene interpretation (3-axis verdict) |
| [#197](https://github.com/happyikas/Aegis-ATV/pull/197) | Demo — coding-AI token-waste detection (5 agents, same task) |
| [#196](https://github.com/happyikas/Aegis-ATV/pull/196) | Demo — multi-agent token-optimization end-to-end |
| [#195](https://github.com/happyikas/Aegis-ATV/pull/195) | ActionAdvice sLLM brain (PR-zeta-head, gap #3) |
| [#194](https://github.com/happyikas/Aegis-ATV/pull/194) | ATMU auto WAL replay on startup (gap #2) |
| [#193](https://github.com/happyikas/Aegis-ATV/pull/193) | ContextMemory rotation (gap #1) |

### v0.5.0 – v0.5.6 — naming alignment + `aegis memory claude-md` (2026-05-12)

| PR | Title |
|---|---|
| [#192](https://github.com/happyikas/Aegis-ATV/pull/192) | `aegis-mvp 0.4.1` deprecation redirect shim on PyPI |
| [#191](https://github.com/happyikas/Aegis-ATV/pull/191) | `aegis memory diff` — reverse-lookup of applied proposals |
| [#190](https://github.com/happyikas/Aegis-ATV/pull/190) | Two more `memory claude-md` miners |
| [#189](https://github.com/happyikas/Aegis-ATV/pull/189) | `aegis-mvp` deprecation playbook correction (post dry-run) |
| [#188](https://github.com/happyikas/Aegis-ATV/pull/188) | `aegis-mvp` PyPI deprecation playbook |
| [#187](https://github.com/happyikas/Aegis-ATV/pull/187) | `aegis memory claude-md --apply N` splices proposals |
| [#186](https://github.com/happyikas/Aegis-ATV/pull/186) | Node 24 workflow opt-in + docs vocab refresh |
| [#185](https://github.com/happyikas/Aegis-ATV/pull/185) | `aegis memory claude-md` proposes real CLAUDE.md edits |
| [#184](https://github.com/happyikas/Aegis-ATV/pull/184) | Top-level CLI restructure to operator vocabulary |
| [#183](https://github.com/happyikas/Aegis-ATV/pull/183) | `0.5.0` — naming alignment to Aegis ATV (Agent Telemetry Vector) |

### v0.4.0 — One-Stop Console (2026-05-12)

| PR | Title |
|---|---|
| [#182](https://github.com/happyikas/Aegis-ATV/pull/182) | `aegis rule add` — Hookify-style natural-language rules (PR-Y1) |
| [#181](https://github.com/happyikas/Aegis-ATV/pull/181) | `0.4.0` release — Dashboard · Tour · Serena integration |
| [#180](https://github.com/happyikas/Aegis-ATV/pull/180) | `aegis tour` — 60-second interactive onboarding (PR-C1) |
| [#179](https://github.com/happyikas/Aegis-ATV/pull/179) | Aegis × Serena integration guide |
| [#178](https://github.com/happyikas/Aegis-ATV/pull/178) | `aegis dashboard` — one-screen TUI (PR-A1) |
| [#177](https://github.com/happyikas/Aegis-ATV/pull/177) | `0.3.3` hotfix — bundle plugin manifest in wheel |

### v0.3.x — earlier (pre-2026-05-12)

| PR / Issue | Landed | Title |
|---|---|---|
| [PR #167](https://github.com/happyikas/Aegis-ATV/pull/167) | 2026-05-11 | CLI install messaging flipped to GA (caught via #151 pre-flight) |
| [PR #166](https://github.com/happyikas/Aegis-ATV/pull/166) | 2026-05-11 | Release `0.3.1` — `aegis-mvp` on PyPI + GHCR (multi-arch). Supersedes the never-published `0.3.0` tag |
| [PR #165](https://github.com/happyikas/Aegis-ATV/pull/165) | 2026-05-11 | post-0.3.0-GA cleanup — drop `@preview` refs + refresh ROADMAP |
| [PR #164](https://github.com/happyikas/Aegis-ATV/pull/164) | 2026-05-11 | `openclaw-plugin 0.3.0` GA — `-preview` suffix lifted (closes [#148](https://github.com/happyikas/Aegis-ATV/issues/148)) |
| [PR #163](https://github.com/happyikas/Aegis-ATV/pull/163) | 2026-05-10 | License gate wired — `--profile pro/cloud`, sidecar install, runtime advisor (LICENSE_KEY.md §9 steps 5-7; closes [#149](https://github.com/happyikas/Aegis-ATV/issues/149)) |
| [PR #162](https://github.com/happyikas/Aegis-ATV/pull/162) | 2026-05-10 | `docs/THREAT_MODEL.md` — STRIDE walk + auditor checklist for the 3rd-party audit |
| [PR #161](https://github.com/happyikas/Aegis-ATV/pull/161) | 2026-05-10 | `aegis-mvp 0.3.0` release commit (version bump + CHANGELOG). Tag was missed; superseded by PR #166 → `0.3.1` on PyPI / GHCR. See `CHANGELOG.md [0.3.0]` for the "ghost release" note |
| [PR #160](https://github.com/happyikas/Aegis-ATV/pull/160) | earlier | Load-test harness — `aegis soak` + `aegis bench` |
| [PR #159](https://github.com/happyikas/Aegis-ATV/pull/159) | earlier | Sidecar production hardening — rate limit + size cap + /readyz + graceful shutdown + structured errors |
| [PR #158](https://github.com/happyikas/Aegis-ATV/pull/158) | earlier | Audit log rotation — gzip + time trigger + `aegis audit status/prune` |
| [PR #157](https://github.com/happyikas/Aegis-ATV/pull/157) | earlier | License no-op gate plumbing — Solo Pro / Team / Enterprise |
| [PR #156](https://github.com/happyikas/Aegis-ATV/pull/156) | earlier | Release pipeline — PyPI + GHCR multi-arch + slim sdist |
| [PR #155](https://github.com/happyikas/Aegis-ATV/pull/155) | earlier | Gap C — Coach burn-in 3-tuple `(aid, role, provider)` (closes [#146](https://github.com/happyikas/Aegis-ATV/issues/146)) |
| [PR #154](https://github.com/happyikas/Aegis-ATV/pull/154) | earlier | Gap B — per-aid vLLM endpoint config + multi-server scrape (closes [#145](https://github.com/happyikas/Aegis-ATV/issues/145)) |
| [PR #144](https://github.com/happyikas/Aegis-ATV/pull/144) | 2026-05-09 | `PRICING.md` + `docs/LICENSE_KEY.md` (free / paid boundary) |
| [PR #143](https://github.com/happyikas/Aegis-ATV/pull/143) | 2026-05-09 | OpenClaw plugin E2E test against real Aegis sidecar |
| [PR #142](https://github.com/happyikas/Aegis-ATV/pull/142) | 2026-05-09 | Gap A: `aegis report --by-aid-and-provider` cross-grouping |
| [PR #141](https://github.com/happyikas/Aegis-ATV/pull/141) | 2026-05-09 | OpenClaw track step 8 ✅ — npm package published as `0.2.0-preview.2` |
| [PR #140](https://github.com/happyikas/Aegis-ATV/pull/140) | 2026-05-09 | rename npm scope `@openclaw` → `@happyikas` |
| [PR #139](https://github.com/happyikas/Aegis-ATV/pull/139) | 2026-05-09 | `0.2.0-preview.2` publish blocker fixes |
| [PR #138](https://github.com/happyikas/Aegis-ATV/pull/138) | 2026-05-09 | npm publish prep — `0.2.0-preview.1` |
| [PR #136](https://github.com/happyikas/Aegis-ATV/pull/136) | 2026-05-08 | `aegis report --by-provider` |
| [PR #134](https://github.com/happyikas/Aegis-ATV/pull/134) | 2026-05-08 | `aegis report --by-channel` |
| [PR #133](https://github.com/happyikas/Aegis-ATV/pull/133) | 2026-05-08 | `aegis report --by-aid` |

---

## Materializing the GitHub Project board

The repo maintainer can spin up a board view of these issues with:

```bash
# One-time (your gh token needs project + read:project scopes)
gh auth refresh -s project,read:project

# Create the project under the user account
gh project create --owner happyikas --title "Aegis ATV Roadmap"
# → returns a project number, e.g. "1"

# Add the issues to the board
PROJECT=1   # substitute
for n in 145 146 147 148 149 150 151; do
  gh project item-add $PROJECT \
    --owner happyikas \
    --url "https://github.com/happyikas/Aegis-ATV/issues/$n"
done

# Optional: configure status field
# gh project field-create $PROJECT --owner happyikas \
#   --name "Status" --data-type "SINGLE_SELECT" \
#   --single-select-options "Backlog,Now,Next,Done"
```

The result is a kanban-style view at
`https://github.com/users/happyikas/projects/<num>` that mirrors this
file. **This file remains the source of truth** so contributors who
hit a 404 on the project URL still have somewhere to read.

---

## How to propose a roadmap change

1. Open an issue (use the labels `roadmap` + `enhancement`)
2. Reference the issue from a draft PR if you're already prototyping
3. We update this file as part of merging the corresponding PR

We deliberately do *not* gate roadmap edits on a triage process — if
you have a clean idea, send a PR that adds a row to the right
section. The maintainer will rebalance priorities at merge time.
