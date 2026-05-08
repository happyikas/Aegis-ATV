---
description: Aegis 5-line risk summary of recent Claude Code tool calls
allowed-tools: Bash
---

Run this exact bash command and show the user its output verbatim, with no commentary unless the output indicates an error or absence:

```bash
{AEGIS_CMD} report --since 24h
```

If the output mentions "no audit log", suggest the user run `/aegis-help` to learn how to start logging. Otherwise, just present the output as-is.
