---
description: Cryptographically verify the Aegis audit chain (SHA3 + Ed25519)
allowed-tools: Bash
---

Run this exact bash command and show the user its output verbatim:

```bash
{AEGIS_CMD} verify-audit
```

If the output shows "FAILED — chain broken", emphasize that this means the audit log has been mutated post-write — the user should investigate. If the output shows "signing pubkey: not configured", briefly mention that `/aegis-help` can show how to enable Ed25519 signing.
