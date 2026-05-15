"""ContextMemory — append-only ATV analytics store.

ContextMemory is a **software emulation** of the planned CXL SSD /
Computational SSD persistent context layer. The same schema that
this file-backed store uses is the silicon spec: each record is
self-contained, append-only, and shaped so a near-storage compute
engine can do filter + aggregate without ever moving raw data to
host RAM. Until the silicon ships, a plain JSONL file fronted by
the same query API serves as a faithful emulator.

Architecture
------------

    ┌───────────────────┐
    │  Firewall verdict │   ← per tool call
    └─────────┬─────────┘
              ▼
    ┌──────────────────────────────────────────┐
    │  Two parallel writers (defensive)        │
    │                                          │
    │  audit.jsonl       ← SHA3+Ed25519 chain  │
    │                      (tamper-evidence)   │
    │                                          │
    │  context_memory    ← ATV-centric analytics
    │     .jsonl           (cost/perf/security │
    │                       roll-ups)          │
    └──────────────────────────────────────────┘

ContextMemory is **separate from the audit log** because the two
have different concerns:

* audit.jsonl is a cryptographic chain — append-only, hash-linked,
  Ed25519-signed; optimised for provable history.
* context_memory.jsonl is an analytics store — denormalised flat
  records; optimised for ``aegis doctor`` style aggregation queries.

A future CXL/Computational SSD device sees only the ContextMemory
schema. Audit chain stays on host (the silicon doesn't need to
verify itself).

Privacy
-------
ContextMemory records carry the same projection as the audit log:
tool_name, decision, latency, cost — never tool args, never model
output. This mirrors ``aegis.burnin.shadow`` recording semantics —
we do not collect content, only metadata.

Public API
----------
* :func:`append` — write one record (defensive, never raises)
* :func:`iter_records` — stream the store
* :func:`read_window` — slice by time window
* :data:`ContextMemoryRecord` — schema dataclass
"""

from __future__ import annotations

from aegis.context_memory.record import ContextMemoryRecord
from aegis.context_memory.writer import (
    append,
    context_memory_path,
    iter_records,
    read_all,
    read_window,
)

__all__ = [
    "ContextMemoryRecord",
    "append",
    "context_memory_path",
    "iter_records",
    "read_all",
    "read_window",
]
