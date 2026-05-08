---
description: Postmortem timeline for one Claude Code session (use $ARGUMENTS to pick selector)
allowed-tools: Bash
argument-hint: <selector or 'last'>
---

The user wants a forensic timeline. Use this exact bash command, substituting the user's argument (defaulting to `last`):

```bash
{AEGIS_CMD} forensic ${ARGUMENTS:-last} --limit 30
```

Show the output verbatim. The timeline is already chronologically ordered with reason / step traces / advisor signals — no extra explanation needed unless something looks abnormal (e.g., many BLOCKs in a row, or an audit chain inconsistency).
