"""Pre-action checkpoint (patent ¶[0063E]).

Before a high-blast tool invocation is released, the ATMU records a
checkpoint comprising one or more of:
    - agent working-memory snapshot
    - context-window digest
    - file-system pre-image hash
    - capability manifest version
    - policy version
    - AID-to-region authorization table version

For T2 the checkpoint is a hash over the bundle so it can be replayed
or referenced from rollback events. T3 stores the actual snapshot in
non-volatile CSD memory.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from aegis.schema import ATVInput

# Per ¶[0063E] – tools whose blast warrants a checkpoint before release.
HIGH_BLAST_THRESHOLD = 7


def make_checkpoint(inp: ATVInput, blast_radius: int) -> dict[str, Any] | None:
    """Compute a checkpoint manifest for an intent. Returns None for
    low-blast calls — those don't warrant the bookkeeping cost."""
    if blast_radius < HIGH_BLAST_THRESHOLD:
        return None

    bundle = {
        "aid": inp.header.aid,
        "tenant_id": inp.header.tenant_id,
        "agent_state": inp.agent_state_text,
        "plan": inp.plan_text,
        "memory_fp": inp.memory_fingerprint or "",
        "tier_profile": inp.header.tier_profile,
        "capability_manifest": sorted(inp.capability_manifest),
    }
    digest = hashlib.sha3_256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "checkpoint_id": str(uuid.uuid4()),
        "manifest_hash": digest,
        "ts_ns": time.time_ns(),
        "fields_present": list(bundle.keys()),
    }
