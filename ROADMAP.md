# Aegis ATV — Roadmap

> Public roadmap kept in this file *and* mirrored to a GitHub Project
> board (the board is the canonical view for triage; this file is the
> stable URL for outsiders). Last refreshed: 2026-05-09.

For the most-recent issue activity see
[the issues list](https://github.com/happyikas/Aegis-ATV/issues).

---

## Now — in flight

| Issue / PR | Title | Status |
|---|---|---|
| [PR #143](https://github.com/happyikas/Aegis-ATV/pull/143) | OpenClaw plugin E2E test against real Aegis sidecar | CI in progress |
| [PR #144](https://github.com/happyikas/Aegis-ATV/pull/144) | `PRICING.md` + `docs/LICENSE_KEY.md` (free / paid boundary) | open |

PR #142 (Gap A — `aegis report --by-aid-and-provider`) merged 2026-05-09.

---

## Next — multi-agent + multi-LLM follow-on

These are the three remaining gaps from the multi-agent + multi-LLM
cross-grouping review. PR #142 (Gap A) is the report-side; these
extend Gap A to **infrastructure**, **learning**, and **cross-agent
provenance**.

| Priority | Issue | One-line | Blocker |
|---|---|---|---|
| 🟢 high | [#145 — Gap B](https://github.com/happyikas/Aegis-ATV/issues/145) | per-aid vLLM endpoint config (multi-vLLM-server scrape) | none — ready to start |
| 🟡 mid | [#146 — Gap C](https://github.com/happyikas/Aegis-ATV/issues/146) | per-(aid, provider) baseline learning (Coach burn-in) | depends on Gap B for endpoint metrics |
| 🔴 low | [#147 — Gap D](https://github.com/happyikas/Aegis-ATV/issues/147) | inter-agent edge tracking (`inter_agent_edges` populate) | depends on OpenClaw runtime cooperation |

---

## OpenClaw plugin lifecycle

| Issue | What's there | When |
|---|---|---|
| [#148](https://github.com/happyikas/Aegis-ATV/issues/148) | Lift `-preview` suffix → publish `@happyikas/openclaw-plugin-aegis@0.3.0` | E2E CI runs green ≥ 7 days on `main` (gated by PR #143) |
| [#150](https://github.com/happyikas/Aegis-ATV/issues/150) | ClawHub marketplace registration | paused until ClawHub goes public |

---

## Business model

| Issue | What's there | Gating |
|---|---|---|
| [#149](https://github.com/happyikas/Aegis-ATV/issues/149) | Implement license-key validation runtime gate | ≥ 3 paying design partners willing to test activation |
| [#151](https://github.com/happyikas/Aegis-ATV/issues/151) | Show HN go-live prep | the pre-flight checklist in the issue |

---

## Done — recent

(For the full release log see [`CHANGELOG.md`](CHANGELOG.md).)

| PR | Landed | Title |
|---|---|---|
| [#142](https://github.com/happyikas/Aegis-ATV/pull/142) | 2026-05-09 | Gap A: `aegis report --by-aid-and-provider` cross-grouping |
| [#141](https://github.com/happyikas/Aegis-ATV/pull/141) | 2026-05-09 | OpenClaw track step 8 ✅ — npm package published |
| [#140](https://github.com/happyikas/Aegis-ATV/pull/140) | 2026-05-09 | rename npm scope `@openclaw` → `@happyikas` |
| [#139](https://github.com/happyikas/Aegis-ATV/pull/139) | 2026-05-09 | `0.2.0-preview.2` publish blocker fixes |
| [#138](https://github.com/happyikas/Aegis-ATV/pull/138) | 2026-05-09 | npm publish prep — `0.2.0-preview.1` |
| [#136](https://github.com/happyikas/Aegis-ATV/pull/136) | 2026-05-08 | `aegis report --by-provider` |
| [#134](https://github.com/happyikas/Aegis-ATV/pull/134) | 2026-05-08 | `aegis report --by-channel` |
| [#133](https://github.com/happyikas/Aegis-ATV/pull/133) | 2026-05-08 | `aegis report --by-aid` |

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
