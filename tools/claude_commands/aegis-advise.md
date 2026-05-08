---
description: Live cost / performance / security advisor recommendations
allowed-tools: Bash
---

Run this exact bash command and show the user its output verbatim:

```bash
{AEGIS_CMD} advise --since 24h
```

If the output says "no advisor recommendations" with a hint to enable `--profile pro` or `--profile cloud`, briefly explain that recommendations require the advisor pipeline (which is OFF in the default `free` profile). Otherwise present the recommendations as-is, and offer to dig deeper into any of them with `/aegis-forensic <trace>`.
