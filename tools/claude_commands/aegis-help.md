---
description: List Aegis slash commands + show what each does
allowed-tools: []
---

Show the user this list of available Aegis slash commands inside Claude Code, formatted as a clean bulleted list:

- `/aegis-report` — 5-line risk summary of recent tool calls (last 24 h)
- `/aegis-verify` — cryptographically verify the audit chain (SHA3 + Ed25519 if configured)
- `/aegis-advise` — live cost / performance / security advisor recommendations (requires `--profile pro` or `--profile cloud`)
- `/aegis-forensic [selector]` — postmortem timeline for one session (default: `last`)
- `/aegis-help` — this list

Then add a one-line tip:

> Ed25519 signing is opt-in. To enable: run `aegis audit-key init` once in your terminal, then every subsequent audit append is signed.

Keep the response under 200 words. No code-fence tables, just the bulleted list.
