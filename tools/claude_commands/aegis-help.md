---
description: List Aegis slash commands + show what each does
allowed-tools: []
---

Show the user this list of available Aegis slash commands inside Claude Code, formatted as a clean bulleted list grouped by Aegis ATV's three named features (Coach / Live / Doctor):

**📊 ATV Live — real-time monitoring (cost / perf / security)**
- `/aegis-report` — 5-line risk summary of recent tool calls (last 24 h)

**🔧 ATV Doctor — diagnose / advise / rollback**
- `/aegis-advise` — live cost / performance / security advisor recommendations (requires `--profile pro` or `--profile cloud`)
- `/aegis-forensic [selector]` — postmortem timeline for one session (default: `last`)

**Neutral infrastructure**
- `/aegis-verify` — cryptographically verify the audit chain (SHA3 + Ed25519 if configured)
- `/aegis-help` — this list

Then add a one-line tip:

> Ed25519 signing is opt-in. To enable: run `aegis audit-key init` once in your terminal, then every subsequent audit append is signed.
> Full user manuals (Korean): see `docs/manuals/` — COACH_MANUAL.ko.md, LIVE_MANUAL.ko.md, DOCTOR_MANUAL.ko.md.

Keep the response under 220 words. No code-fence tables, just the bulleted list grouped by emoji-prefixed bucket headings.
